"""Command line: `dbt-fault-drill run` and `dbt-fault-drill list`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, read_config, read_project
from .faults import CATALOG
from .report import summary, to_json, to_markdown
from .runner import BaselineFailed, drill


def _list(_: argparse.Namespace) -> int:
    width = max(len(f.name) for f in CATALOG)
    for f in CATALOG:
        needs = ", ".join(f.needs) or "-"
        print(f"{f.name:<{width}}  needs: {needs:<12} {f.summary}")
    return 0


def _run(a: argparse.Namespace) -> int:
    root = Path(a.project_dir).resolve()
    try:
        info = read_project(root)
        cfg = read_config(Path(a.config) if a.config else root / "fault_drill.yml")
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if a.select is not None:
        cfg.select = a.select or None
    if a.exclude is not None:
        cfg.exclude = a.exclude or None
    if a.faults:
        wanted = [x.strip() for x in a.faults.split(",") if x.strip()]
        unknown = [w for w in wanted if w not in cfg.faults and w not in cfg.skipped]
        if unknown:
            print(f"unknown or unconfigured faults: {unknown}", file=sys.stderr)
            return 2
        cfg.faults = [f for f in cfg.faults if f in wanted]

    print(f"dbt-fault-drill {__version__}: {len(cfg.faults)} faults on seed '{cfg.seed}' "
          f"in {root.name}", file=sys.stderr)
    try:
        rep = drill(info, cfg, jobs=a.jobs, dbt_bin=a.dbt,
                    keep=Path(a.keep).resolve() if a.keep else None)
    except BaselineFailed as e:
        r = e.result
        print("baseline failed: the project must build cleanly before faults are planted.",
              file=sys.stderr)
        for n in r.failing_tests + r.warning_tests + r.errored_nodes:
            print(f"  - {n}", file=sys.stderr)
        if r.detail:
            print(r.detail, file=sys.stderr)
        if cfg.select:
            print("hint: every run starts from an empty database, so --select must include "
                  "every parent of the selected models and tests (for example '+model+' "
                  "or '@model').", file=sys.stderr)
        return 3
    except (FileNotFoundError, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    md = to_markdown(rep, title=a.title)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
    else:
        print(md)
    if a.json:
        Path(a.json).write_text(to_json(rep), encoding="utf-8")
    s = summary(rep)
    print(f"stopped {s['stopped']}/{s['planted']}, silent {s['silent']}", file=sys.stderr)
    if s["tool_errors"]:
        print(f"tool errors: {s['tool_errors']}", file=sys.stderr)
        return 4
    if not s["planted"]:
        print("no fault could be planted in this data; nothing was measured", file=sys.stderr)
        return 5
    if a.fail_under is not None and s["planted"] and s["stopped"] / s["planted"] * 100 < a.fail_under:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dbt-fault-drill",
                                description="Plant realistic faults in a dbt seed and measure "
                                            "how many your tests stop.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list the fault catalog").set_defaults(fn=_list)

    r = sub.add_parser("run", help="run the drill against a dbt-duckdb project")
    r.add_argument("--project-dir", default=".", help="dbt project root (default: .)")
    r.add_argument("--config", help="drill config (default: <project>/fault_drill.yml)")
    r.add_argument("--select", help="override dbt --select ('' clears it)")
    r.add_argument("--exclude", help="override dbt --exclude ('' clears it)")
    r.add_argument("--faults", help="comma-separated subset of faults")
    r.add_argument("--jobs", type=int, default=4, help="faults run in parallel (default 4)")
    r.add_argument("--out", help="write the Markdown report here instead of stdout")
    r.add_argument("--json", help="also write a JSON report here")
    r.add_argument("--title", default="Fault drill report")
    r.add_argument("--fail-under", type=float,
                   help="exit 1 if the stopped percentage is below this (for CI)")
    r.add_argument("--keep", help="keep the per-fault project copies in this directory")
    r.add_argument("--dbt", default="dbt", help="dbt executable (default: dbt)")
    r.set_defaults(fn=_run)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except Exception as e:   # keep exit 1 for --fail-under only
        print(f"internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
