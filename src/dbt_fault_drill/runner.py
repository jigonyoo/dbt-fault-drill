"""Copies the project, plants one fault per copy, runs `dbt build`, reads the verdict.

The original project is never modified. Each fault gets its own temporary copy
and its own DuckDB file, so runs are independent and can go in parallel.
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ConfigError, DrillConfig, ProjectInfo
from .faults import BY_NAME, Context, FaultNotApplicable

# Outcome labels
CAUGHT = "caught"            # at least one dbt test failed
LOAD_ERROR = "load_error"    # no test failed, but a seed or model errored (loud stop)
WARN_ONLY = "warn_only"      # only warn-severity tests fired; the build still passed
MISSED = "missed"            # dbt build passed: the bad batch went through silently
NOT_APPLICABLE = "not_applicable"
TOOL_ERROR = "tool_error"    # dbt failed outside any seed/model/test, or wrote no results

STOPPED = {CAUGHT, LOAD_ERROR}
SILENT = {WARN_ONLY, MISSED}

_ROOT_ONLY = {"target", "logs", ".git", "__pycache__", ".venv", "venv", "dbt_packages_cache"}
_ANYWHERE = shutil.ignore_patterns("*.duckdb", "*.duckdb.wal", "__pycache__")


def _ignore_for(root: Path):
    """Skip build output and environments at the project root only.

    A folder called `logs` or `target` deeper in the tree (models/logs/...) is
    project code and must be copied.
    """
    root = Path(root).resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = set(_ANYWHERE(directory, names))
        if Path(directory).resolve() == root:
            skipped |= {n for n in names if n in _ROOT_ONLY}
        return skipped
    return ignore


@dataclass
class RunResult:
    fault: str
    outcome: str
    planted: str = ""
    failing_tests: list[str] = field(default_factory=list)
    warning_tests: list[str] = field(default_factory=list)
    errored_nodes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    detail: str = ""
    impact: float | None = None      # SUM(impact column) after the build, if it was built


# --- CSV helpers -----------------------------------------------------------

BOM = b"\xef\xbb\xbf"


def has_bom(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(3) == BOM


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    # utf-8-sig drops a leading byte-order mark, so the first column name stays clean
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        return header, [dict(r) for r in reader]


def write_csv(path: Path, header: list[str], rows: list[dict[str, str]],
              bom: bool = False) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in header})
    path.write_text(buf.getvalue(), encoding="utf-8-sig" if bom else "utf-8")


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def first_error_line(tail: str) -> str:
    """The most informative line of a dbt failure, for the report."""
    lines = [l.strip() for l in tail.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if "Error" in line and i + 1 < len(lines) and not lines[i + 1].startswith("["):
            nxt = lines[i + 1]
            return f"{line} {nxt}" if len(line) < 120 else line
    for line in lines:
        if "Error" in line or "error" in line:
            return line
    return lines[-1] if lines else "dbt produced no output"


# --- dbt invocation --------------------------------------------------------

def _write_profiles(dest: Path, profile: str, db_path: Path, threads: int) -> Path:
    pdir = dest / "_drill_profiles"
    pdir.mkdir(exist_ok=True)
    body = {profile: {"target": "drill",
                      "outputs": {"drill": {"type": "duckdb", "path": str(db_path),
                                            "threads": threads}}}}
    (pdir / "profiles.yml").write_text(yaml.safe_dump(body))
    return pdir


def _short(unique_id: str) -> str:
    # test.shop.accepted_values_stg_orders_status__placed__shipped.3f2a -> accepted_values_stg_orders_status__placed__shipped
    parts = unique_id.split(".")
    return parts[2] if len(parts) >= 3 else unique_id


def run_dbt_build(project: Path, profile: str, cfg: DrillConfig, threads: int = 1,
                  dbt_bin: str = "dbt") -> tuple[int, dict | None, str]:
    db = project / "drill.duckdb"
    pdir = _write_profiles(project, profile, db, threads)
    cmd = [dbt_bin, "build", "--project-dir", str(project), "--profiles-dir", str(pdir),
           "--target", "drill",
           "--target-path", str(project / "target"), "--log-path", str(project / "logs")]
    if cfg.select:
        cmd += ["--select", cfg.select]
    if cfg.exclude:
        cmd += ["--exclude", cfg.exclude]
    env = dict(os.environ, DBT_SEND_ANONYMOUS_USAGE_STATS="false", DO_NOT_TRACK="1")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=project)
    rr_path = project / "target" / "run_results.json"
    results = json.loads(rr_path.read_text()) if rr_path.exists() else None
    tail = "\n".join(_ANSI.sub("", proc.stdout + proc.stderr).strip().splitlines()[-25:])
    return proc.returncode, results, tail


def classify(results: dict | None, returncode: int = 0,
             standing_warnings: frozenset[str] = frozenset()
             ) -> tuple[str, list[str], list[str], list[str]]:
    """Decide the outcome from run_results.json and dbt's exit code.

    A non-zero exit with no failing node (an on-run-end hook, a crash after the
    nodes ran) is a tool error, never a pass: the artifacts alone do not prove
    the build succeeded. A failing hook (an `operation.` node) is also a tool
    error, not a load error: it says nothing about whether the data was stopped.
    Warnings that already fire on the clean baseline are ignored.
    """
    if results is None:
        return TOOL_ERROR, [], [], []
    failing, warning, errored, operations = [], [], [], []
    for r in results.get("results", []):
        uid, status = r.get("unique_id", ""), r.get("status")
        is_test = uid.startswith("test.")
        if is_test and status in ("fail", "error"):
            failing.append(_short(uid))
        elif is_test and status == "warn":
            if _short(uid) not in standing_warnings:
                warning.append(_short(uid))
        elif uid.startswith("operation.") and status == "error":
            operations.append(_short(uid))
        elif not is_test and status == "error":
            errored.append(_short(uid))
    if failing:
        return CAUGHT, failing, warning, errored
    if errored:
        return LOAD_ERROR, failing, warning, errored
    if returncode != 0 or operations:
        return TOOL_ERROR, failing, warning, errored + operations
    if warning:
        return WARN_ONLY, failing, warning, errored
    return MISSED, failing, warning, errored


def _relation(project: Path, model: str) -> tuple[str | None, str]:
    """(schema, table) dbt built for `model`, honouring custom schemas and aliases."""
    path = project / "target" / "manifest.json"
    if path.exists():
        nodes = json.loads(path.read_text()).get("nodes", {})
        matches = [n for n in nodes.values()
                   if n.get("resource_type") == "model" and n.get("name") == model]
        if matches:
            # Versioned models: measure the latest version, as ref() would resolve it.
            latest = [n for n in matches if n.get("version") is None
                      or str(n.get("version")) == str(n.get("latest_version"))]
            node = (latest or matches)[0]
            return node.get("schema"), node.get("alias") or model
    return None, model


def measure_impact(project: Path, cfg: DrillConfig) -> float | None:
    """SUM(cfg.impact_column) over cfg.impact_model in this copy's DuckDB file.

    Returns None when not configured, or when the model was not built (for example
    because an upstream test failed and dbt skipped it).
    """
    if not (cfg.impact_model and cfg.impact_column):
        return None
    db = project / "drill.duckdb"
    if not db.exists():
        return None
    import duckdb   # installed with dbt-duckdb

    schema, table = _relation(project, cfg.impact_model)
    con = duckdb.connect(str(db), read_only=True)
    try:
        if schema is None:   # not in the manifest: fall back to the table name
            row = con.execute(
                "select table_schema from information_schema.tables where table_name = ? "
                "order by table_schema limit 1", [table]).fetchone()
        else:
            row = con.execute(
                "select table_schema from information_schema.tables "
                "where table_schema = ? and table_name = ?", [schema, table]).fetchone()
        if row is None:
            return None
        q = f'select sum("{cfg.impact_column}") from "{row[0]}"."{table}"'
        val = con.execute(q).fetchone()[0]
        return float(val) if val is not None else 0.0
    finally:
        con.close()


# --- one fault -------------------------------------------------------------

def _copy_project(info: ProjectInfo, workdir: Path, name: str) -> Path:
    dest = workdir / name
    shutil.copytree(info.root, dest, ignore=_ignore_for(info.root))
    return dest


def _context(info: ProjectInfo, cfg: DrillConfig, fault: str) -> Context:
    known: frozenset[str] = frozenset()
    if cfg.foreign_key_seed:
        _, parent_rows = read_csv(info.seed_file(cfg.foreign_key_seed))
        ref = cfg.foreign_key_ref_column or cfg.roles.get("foreign_key", "")
        known = frozenset(r.get(ref, "") for r in parent_rows)
    # Seed the RNG per fault so results do not depend on run order.
    rng = random.Random(f"{cfg.rng_seed}:{fault}")
    return Context(roles=cfg.roles, known_fk_values=known, rng=rng)


def run_one(info: ProjectInfo, cfg: DrillConfig, fault: str, workdir: Path,
            dbt_bin: str = "dbt", standing_warnings: frozenset[str] = frozenset()) -> RunResult:
    t0 = time.monotonic()
    seed_rel = info.seed_file(cfg.seed).relative_to(info.root)
    header, rows = read_csv(info.root / seed_rel)
    try:
        new_rows, note = BY_NAME[fault].apply(rows, _context(info, cfg, fault))
    except FaultNotApplicable as e:
        return RunResult(fault, NOT_APPLICABLE, detail=str(e))
    except Exception as e:   # a fault that cannot handle this data must not abort the drill
        return RunResult(fault, NOT_APPLICABLE, detail=f"could not plant: {type(e).__name__}: {e}")
    if new_rows == rows:   # safety net: a fault that changed nothing was not planted
        return RunResult(fault, NOT_APPLICABLE, detail="the fault left the data unchanged")
    proj = _copy_project(info, workdir, fault)
    write_csv(proj / seed_rel, header, new_rows, bom=has_bom(info.root / seed_rel))
    code, results, tail = run_dbt_build(proj, info.profile, cfg, dbt_bin=dbt_bin)
    outcome, failing, warning, errored = classify(results, code, standing_warnings)
    return RunResult(fault, outcome, planted=note, failing_tests=failing,
                     warning_tests=warning, errored_nodes=errored,
                     seconds=round(time.monotonic() - t0, 1),
                     detail=tail if outcome == TOOL_ERROR else "",
                     impact=measure_impact(proj, cfg))


@dataclass
class DrillReport:
    project: str
    seed: str
    select: str | None
    exclude: str | None
    baseline: RunResult
    results: list[RunResult]
    skipped: dict[str, str]
    dbt_version: str
    rng_seed: int
    impact_model: str | None = None
    impact_column: str | None = None
    standing_warnings: list[str] = field(default_factory=list)


def dbt_version(dbt_bin: str = "dbt") -> str:
    try:
        out = subprocess.run([dbt_bin, "--version"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return "dbt not found"
    # dbt prints "Core:\n  - installed: 1.12.5 ..." then "Plugins:\n  - duckdb: 1.11.0 ..."
    parts, section = [], ""
    for raw in out.splitlines():
        line = raw.strip()
        if line.endswith(":") and not line.startswith("-"):
            section = line[:-1].lower()
        elif section == "core" and line.startswith("- installed:"):
            parts.append("dbt-core " + line.split(":", 1)[1].split()[0])
        elif section == "plugins" and line.startswith("- ") and ":" in line:
            name, ver = line[2:].split(":", 1)
            parts.append(f"dbt-{name.strip()} {ver.split()[0]}")
    return ", ".join(parts) or "unknown dbt version"


def check_columns(info: ProjectInfo, cfg: DrillConfig) -> None:
    """Every mapped role must name a real column, or faults would silently skip."""
    header, _ = read_csv(info.seed_file(cfg.seed))
    missing = [f"{role}={col!r}" for role, col in cfg.roles.items() if col not in header]
    if missing:
        raise ConfigError(f"seed {cfg.seed!r} has no column for {', '.join(missing)}; "
                          f"its columns are {header}")
    if cfg.foreign_key_seed and "orphan_foreign_key" in cfg.faults:
        ref = cfg.foreign_key_ref_column or cfg.roles.get("foreign_key", "")
        parent_header, _ = read_csv(info.seed_file(cfg.foreign_key_seed))
        if ref not in parent_header:
            raise ConfigError(f"parent seed {cfg.foreign_key_seed!r} has no column {ref!r}")


class BaselineFailed(Exception):
    def __init__(self, result: RunResult):
        super().__init__("the unmodified project does not build cleanly")
        self.result = result


def drill(info: ProjectInfo, cfg: DrillConfig, jobs: int = 4, dbt_bin: str = "dbt",
          keep: Path | None = None) -> DrillReport:
    if shutil.which(dbt_bin) is None:
        raise FileNotFoundError(f"{dbt_bin!r} not on PATH; install dbt-core and dbt-duckdb")
    check_columns(info, cfg)
    if keep is not None:
        keep = keep.resolve()
        root = info.root.resolve()
        if keep == root or root in keep.parents:
            raise ConfigError(f"--keep directory {keep} is inside the project; "
                              "it would be copied into itself. Choose a path outside it")
        if keep.exists() and not keep.is_dir():
            raise ConfigError(f"--keep path {keep} exists and is not a directory")
        if keep.exists() and any(keep.iterdir()):
            raise ConfigError(f"--keep directory {keep} is not empty; pass a new or empty directory")
    tmp_ctx = tempfile.TemporaryDirectory(prefix="fault-drill-") if keep is None else None
    workdir = Path(tmp_ctx.name) if tmp_ctx else keep
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        t0 = time.monotonic()
        base_proj = _copy_project(info, workdir, "_baseline")
        code, results, tail = run_dbt_build(base_proj, info.profile, cfg, dbt_bin=dbt_bin)
        outcome, failing, warning, errored = classify(results, code)
        baseline = RunResult("baseline", outcome, planted="no fault", failing_tests=failing,
                             warning_tests=warning, errored_nodes=errored,
                             seconds=round(time.monotonic() - t0, 1), detail=tail,
                             impact=measure_impact(base_proj, cfg))
        # The clean build must pass: no failing test, errored node, failing hook or bad
        # exit code. Warnings are allowed; they are recorded as standing warnings and
        # ignored when judging the faults.
        if outcome not in (MISSED, WARN_ONLY):
            raise BaselineFailed(baseline)
        standing = frozenset(warning)
        seeds_run = {r.get("unique_id", "").rsplit(".", 1)[-1] for r in results["results"]
                     if r.get("unique_id", "").startswith("seed.")}
        if cfg.seed not in seeds_run:
            raise ConfigError(f"seed {cfg.seed!r} is not part of the dbt selection "
                              f"(select={cfg.select!r}, exclude={cfg.exclude!r}); "
                              "faults planted there would never be read")
        if cfg.impact_model and baseline.impact is None:
            raise ConfigError(f"impact model {cfg.impact_model!r} was not found after the "
                              "clean build; check the model name and the selection")
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            futures = [pool.submit(run_one, info, cfg, f, workdir, dbt_bin, standing)
                       for f in cfg.faults]
            res = [f.result() for f in futures]
        return DrillReport(project=str(info.root), seed=cfg.seed, select=cfg.select,
                           exclude=cfg.exclude, baseline=baseline, results=res,
                           skipped=dict(cfg.skipped), dbt_version=dbt_version(dbt_bin),
                           rng_seed=cfg.rng_seed, impact_model=cfg.impact_model,
                           impact_column=cfg.impact_column, standing_warnings=sorted(standing))
    finally:
        if tmp_ctx:
            tmp_ctx.cleanup()
