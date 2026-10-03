"""Turns a DrillReport into Markdown and JSON."""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict

from .faults import BY_NAME
from .runner import (first_error_line, CAUGHT, LOAD_ERROR, MISSED, NOT_APPLICABLE, SILENT, STOPPED, TOOL_ERROR,
                     WARN_ONLY, DrillReport)

LABEL = {
    CAUGHT: "stopped by a test",
    LOAD_ERROR: "stopped by a load error",
    WARN_ONLY: "warned only, still loaded",
    MISSED: "went through silently",
    NOT_APPLICABLE: "not applicable",
    TOOL_ERROR: "tool error",
}


def summary(rep: DrillReport) -> dict:
    planted = [r for r in rep.results if r.outcome not in (NOT_APPLICABLE, TOOL_ERROR)]
    stopped = [r for r in planted if r.outcome in STOPPED]
    silent = [r for r in planted if r.outcome in SILENT]
    return {
        "planted": len(planted),
        "stopped": len(stopped),
        "stopped_by_test": sum(r.outcome == CAUGHT for r in planted),
        "stopped_by_load_error": sum(r.outcome == LOAD_ERROR for r in planted),
        "silent": len(silent),
        "silent_faults": [r.fault for r in silent],
        "not_applicable": [r.fault for r in rep.results if r.outcome == NOT_APPLICABLE],
        "tool_errors": [r.fault for r in rep.results if r.outcome == TOOL_ERROR],
        "skipped": rep.skipped,
    }


def to_json(rep: DrillReport) -> str:
    body = {
        "summary": summary(rep),
        "run": {"project": rep.project, "seed": rep.seed, "select": rep.select,
                "exclude": rep.exclude, "rng_seed": rep.rng_seed, "dbt": rep.dbt_version,
                "python": sys.version.split()[0], "platform": platform.platform()},
        "baseline": asdict(rep.baseline),
        "results": [asdict(r) for r in rep.results],
    }
    return json.dumps(body, indent=2)


def _num(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x:,.2f}"


def _delta(value: float | None, base: float | None) -> str:
    if value is None or base is None:
        return ""
    d = value - base
    if abs(d) < 0.005:
        return "0.00"
    pct = f" ({d / base * 100:+.1f}%)" if base else ""
    return f"{d:+,.2f}{pct}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _tests(names: list[str], limit: int = 2) -> str:
    if not names:
        return ""
    shown = ", ".join(f"`{n}`" for n in names[:limit])
    more = len(names) - limit
    return shown + (f" +{more} more" if more > 0 else "")


def to_markdown(rep: DrillReport, title: str = "Fault drill report") -> str:
    s = summary(rep)
    lines = [f"# {title}", ""]
    if s["planted"]:
        lines.append(f"**{s['stopped']} of {s['planted']} planted faults were stopped. "
                     f"{s['silent']} went through silently.**")
    else:
        lines.append("**No faults could be planted.**")
    lines += ["",
              f"- Seed with planted faults: `{rep.seed}`",
              f"- dbt selection: `{rep.select or '(all)'}`"
              + (f", excluding `{rep.exclude}`" if rep.exclude else ""),
              "- Baseline (no fault): build passed with no failing test"
              + (f"; {len(rep.standing_warnings)} warning(s) already fire on clean data and "
                 f"are ignored: {_tests(rep.standing_warnings, 3)}" if rep.standing_warnings
                 else " and no warnings"),
              f"- {rep.dbt_version}, fault RNG seed {rep.rng_seed}",
              "",
              ]
    has_impact = bool(rep.impact_model and rep.impact_column)
    base = rep.baseline.impact
    if has_impact:
        lines.insert(-1, f"- Impact column: change in SUM({rep.impact_column}) of "
                         f"`{rep.impact_model}` against the clean baseline ({_num(base)}); "
                         "blank means the model was not built. 0.00 means the total did not "
                         "move; the rows can still be in the wrong day, currency or group")
        lines += ["| Fault | What was planted | Result | Fired | Impact |",
                  "|---|---|---|---|---|"]
    else:
        lines += ["| Fault | What was planted | Result | Fired |", "|---|---|---|---|"]
    order = {CAUGHT: 2, LOAD_ERROR: 1, WARN_ONLY: 0, MISSED: 0, NOT_APPLICABLE: 3, TOOL_ERROR: 3}
    for r in sorted(rep.results, key=lambda r: (order.get(r.outcome, 9), r.fault)):
        fired = _tests(r.failing_tests) or _tests(r.warning_tests) or _tests(r.errored_nodes)
        what = r.planted or (r.detail.splitlines()[0] if r.detail else "")
        mark = "**" if r.outcome in SILENT else ""
        row = (f"| `{r.fault}` | {_cell(what)} | {mark}{LABEL[r.outcome]}{mark} | "
               f"{_cell(fired)} |")
        if has_impact:
            row += f" {_delta(r.impact, base)} |"
        lines.append(row)
    tool = [r for r in rep.results if r.outcome == TOOL_ERROR]
    if tool:
        lines += ["", "## Tool errors", "",
                  "dbt failed outside any seed, model or test in these runs, so the fault "
                  "was not measured. The JSON report holds the last lines of dbt's output.", ""]
        lines += [f"- `{r.fault}`: {_cell(first_error_line(r.detail))}" for r in tool]
    na = [r for r in rep.results if r.outcome == NOT_APPLICABLE]
    if na:
        lines += ["", "Not planted (data):", ""]
        lines += [f"- `{r.fault}`: {_cell(r.detail)}" for r in na]
    if rep.skipped:
        lines += ["", "Not run (configuration):", ""]
        lines += [f"- `{k}`: {v}" for k, v in rep.skipped.items()]
    if s["silent"]:
        lines += ["", "## Faults that went through, and what would stop them", ""]
        for r in rep.results:
            if r.outcome in SILENT:
                f = BY_NAME[r.fault]
                warned = (f" Only warn-severity tests fired: {_tests(r.warning_tests, 3)}."
                          if r.outcome == WARN_ONLY else "")
                lines.append(f"- `{r.fault}`: {f.summary}.{warned} Would be stopped by "
                             f"{f.catch_hint}.")
    lines += ["", "---",
              "Generated by dbt-fault-drill. Every fault ran on its own throwaway copy of the "
              "project; the original files were not modified."]
    return "\n".join(lines) + "\n"
