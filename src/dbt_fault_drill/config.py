"""Reads fault_drill.yml and the parts of dbt_project.yml the runner needs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .faults import BY_NAME, CATALOG

KNOWN_ROLES = ("key", "amount", "currency", "date", "category", "foreign_key")


class ConfigError(Exception):
    pass


@dataclass
class DrillConfig:
    seed: str
    roles: dict[str, str]
    faults: list[str]
    select: str | None = None
    exclude: str | None = None
    foreign_key_seed: str | None = None
    foreign_key_ref_column: str | None = None
    rng_seed: int = 7
    impact_model: str | None = None      # e.g. fct_daily_revenue
    impact_column: str | None = None     # e.g. revenue (summed)
    skipped: dict[str, str] = field(default_factory=dict)   # fault -> reason


@dataclass
class ProjectInfo:
    root: Path
    profile: str
    seed_paths: list[str]

    def seed_file(self, name: str) -> Path:
        for sp in self.seed_paths:
            for p in sorted((self.root / sp).rglob(f"{name}.csv")):
                return p
        raise ConfigError(f"seed {name!r} not found under {self.seed_paths}")


def read_project(root: Path) -> ProjectInfo:
    path = root / "dbt_project.yml"
    if not path.exists():
        raise ConfigError(f"no dbt_project.yml in {root}")
    data = yaml.safe_load(path.read_text()) or {}
    profile = data.get("profile")
    if not profile:
        raise ConfigError("dbt_project.yml has no 'profile' key")
    return ProjectInfo(root=root, profile=profile,
                       seed_paths=list(data.get("seed-paths") or ["seeds"]))


def read_config(path: Path) -> DrillConfig:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}

    seed = data.get("seed")
    if not seed:
        raise ConfigError("config needs 'seed': the seed (CSV) to plant faults into")

    roles = dict(data.get("roles") or {})
    unknown = set(roles) - set(KNOWN_ROLES)
    if unknown:
        raise ConfigError(f"unknown roles {sorted(unknown)}; known: {list(KNOWN_ROLES)}")

    wanted = data.get("faults", "all")
    if wanted == "all":
        names = [f.name for f in CATALOG]
    else:
        names = list(wanted)
        bad = [n for n in names if n not in BY_NAME]
        if bad:
            raise ConfigError(f"unknown faults {bad}; run `dbt-fault-drill list`")

    cfg = DrillConfig(
        seed=seed,
        roles=roles,
        faults=[],
        select=data.get("select"),
        exclude=data.get("exclude"),
        foreign_key_seed=data.get("foreign_key_seed"),
        foreign_key_ref_column=data.get("foreign_key_ref_column"),
        rng_seed=int(data.get("rng_seed", 7)),
    )
    impact = data.get("impact")
    if impact:
        if not isinstance(impact, dict) or not impact.get("model") or not impact.get("column"):
            raise ConfigError("'impact' needs both 'model' and 'column'")
        cfg.impact_model, cfg.impact_column = impact["model"], impact["column"]
    for n in names:
        missing = [r for r in BY_NAME[n].needs if r not in roles]
        if missing:
            cfg.skipped[n] = f"role(s) not mapped: {', '.join(missing)}"
        else:
            cfg.faults.append(n)
    if "foreign_key" in roles and "orphan_foreign_key" in cfg.faults and not cfg.foreign_key_seed:
        cfg.faults.remove("orphan_foreign_key")
        cfg.skipped["orphan_foreign_key"] = "foreign_key_seed not set"
    return cfg
