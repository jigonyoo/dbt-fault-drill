from pathlib import Path

import pytest

from dbt_fault_drill.config import ConfigError, read_config, read_project
from dbt_fault_drill.report import summary, to_json, to_markdown
from dbt_fault_drill.runner import (CAUGHT, LOAD_ERROR, MISSED, NOT_APPLICABLE, WARN_ONLY,
                                    DrillReport, RunResult, classify, read_csv, write_csv)


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "fault_drill.yml"
    p.write_text(text)
    return p


def test_config_skips_faults_whose_roles_are_missing(tmp_path):
    cfg = read_config(write(tmp_path, "seed: orders\nroles: {key: id}\n"))
    assert "duplicate_key" in cfg.faults and "empty_batch" in cfg.faults
    assert "absurd_amount" in cfg.skipped
    assert "role(s) not mapped: amount" in cfg.skipped["absurd_amount"]


def test_config_orphan_needs_parent_seed(tmp_path):
    cfg = read_config(write(tmp_path, "seed: o\nroles: {foreign_key: cid}\n"))
    assert "orphan_foreign_key" in cfg.skipped


@pytest.mark.parametrize("text, msg", [
    ("roles: {}\n", "needs 'seed'"),
    ("seed: o\nroles: {price: p}\n", "unknown roles"),
    ("seed: o\nfaults: [nope]\n", "unknown faults"),
    ("seed: o\nimpact: {model: m}\n", "'impact' needs"),
])
def test_config_errors(tmp_path, text, msg):
    with pytest.raises(ConfigError, match=msg):
        read_config(write(tmp_path, text))


def test_read_project_needs_profile(tmp_path):
    (tmp_path / "dbt_project.yml").write_text("name: x\n")
    with pytest.raises(ConfigError, match="profile"):
        read_project(tmp_path)


def test_csv_roundtrip_keeps_header_and_quotes(tmp_path):
    p = tmp_path / "s.csv"
    rows = [{"a": "1", "b": "x, y"}, {"a": "2", "b": 'say "hi"'}]
    write_csv(p, ["a", "b"], rows)
    header, back = read_csv(p)
    assert header == ["a", "b"] and back == rows
    write_csv(p, ["a", "b"], [])
    assert read_csv(p) == (["a", "b"], [])


def rr(*items):
    return {"results": [{"unique_id": u, "status": s} for u, s in items]}


@pytest.mark.parametrize("results, expected", [
    (rr(("seed.p.orders", "success"), ("test.p.unique_x.ab", "pass")), MISSED),
    (rr(("seed.p.orders", "success"), ("test.p.unique_x.ab", "fail")), CAUGHT),
    (rr(("seed.p.orders", "error"), ("model.p.stg", "skipped")), LOAD_ERROR),
    (rr(("test.p.range.ab", "warn")), WARN_ONLY),
    (rr(("model.p.stg", "error"), ("test.p.t.ab", "fail")), CAUGHT),
])
def test_classify(results, expected):
    assert classify(results)[0] == expected


def test_classify_names_failing_tests():
    outcome, failing, _, _ = classify(rr(("test.shop.unique_stg_orders_order_id.abc", "fail")))
    assert failing == ["unique_stg_orders_order_id"]


def make_report(results, impact=True):
    base = RunResult("baseline", MISSED, planted="no fault", impact=100.0 if impact else None)
    return DrillReport(project="p", seed="orders", select=None, exclude="tag:contract",
                       baseline=base, results=results, skipped={"x": "role(s) not mapped: amount"},
                       dbt_version="dbt-core 1.x", rng_seed=7,
                       impact_model="fct" if impact else None,
                       impact_column="revenue" if impact else None)


def test_summary_counts_stopped_and_silent():
    rep = make_report([
        RunResult("duplicate_key", CAUGHT, "a", failing_tests=["unique_x"], impact=None),
        RunResult("date_format_drift", LOAD_ERROR, "b", errored_nodes=["orders"]),
        RunResult("absurd_amount", MISSED, "c", impact=150.0),
        RunResult("stale_batch", WARN_ONLY, "d", warning_tests=["fresh"], impact=100.0),
        RunResult("null_key", NOT_APPLICABLE, detail="seed has no rows"),
    ])
    s = summary(rep)
    assert (s["planted"], s["stopped"], s["silent"]) == (4, 2, 2)
    assert s["not_applicable"] == ["null_key"]


def test_markdown_shows_impact_and_hints_and_escapes_pipes():
    rep = make_report([
        RunResult("absurd_amount", MISSED, "amount 1 -> 45000000 | on one row", impact=150.0),
        RunResult("duplicate_key", CAUGHT, "x", failing_tests=["unique_x"]),
    ])
    md = to_markdown(rep)
    assert "**1 of 2 planted faults were stopped. 1 went through silently.**" in md
    assert "+50.00 (+50.0%)" in md
    assert "\\| on one row" in md
    assert "Would be stopped by a range test on the amount." in md
    assert "`x`: role(s) not mapped: amount" in md


def test_markdown_without_impact_has_four_columns():
    md = to_markdown(make_report([RunResult("duplicate_key", CAUGHT, "x")], impact=False))
    assert "| Fault | What was planted | Result | Fired |" in md
    assert "Impact" not in md


def test_json_is_complete():
    import json
    body = json.loads(to_json(make_report([RunResult("duplicate_key", CAUGHT, "x")])))
    assert body["summary"]["stopped"] == 1
    assert body["results"][0]["fault"] == "duplicate_key"
    assert body["run"]["exclude"] == "tag:contract"


def test_nonzero_exit_without_failing_node_is_a_tool_error_not_a_pass():
    from dbt_fault_drill.runner import TOOL_ERROR
    ok = rr(("seed.p.orders", "success"), ("test.p.unique_x.ab", "pass"))
    assert classify(ok, 0)[0] == MISSED
    assert classify(ok, 2)[0] == TOOL_ERROR
    assert classify(rr(("test.p.unique_x.ab", "fail")), 1)[0] == CAUGHT


def test_bom_is_read_clean_and_written_back(tmp_path):
    from dbt_fault_drill.runner import has_bom
    p = tmp_path / "s.csv"
    p.write_bytes(b"\xef\xbb\xbforder_id,amount\n1,2\n")
    header, rows = read_csv(p)
    assert header == ["order_id", "amount"]
    write_csv(p, header, rows, bom=True)
    assert has_bom(p) and read_csv(p) == (header, rows)


def test_failing_hook_operation_is_a_tool_error_not_a_load_error():
    from dbt_fault_drill.runner import TOOL_ERROR
    res = rr(("seed.p.orders", "success"), ("test.p.unique_x.ab", "pass"),
             ("operation.p.p-on-run-end-0", "error"))
    assert classify(res, 1)[0] == TOOL_ERROR
    assert classify(res, 0)[0] == TOOL_ERROR


def test_standing_warnings_are_ignored():
    res = rr(("test.p.range_x.ab", "warn"))
    assert classify(res, 0, frozenset({"range_x"}))[0] == MISSED
    assert classify(res, 0)[0] == WARN_ONLY


def test_unchanged_data_is_never_counted_as_planted(tmp_path, monkeypatch):
    from dbt_fault_drill import faults
    from dbt_fault_drill.config import DrillConfig, ProjectInfo
    from dbt_fault_drill.runner import run_one
    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds" / "orders.csv").write_text("id,amount\n1,0.00\n")
    noop = faults.Fault("noop", (), "changes nothing", lambda rows, c: ([dict(r) for r in rows], "x"))
    monkeypatch.setitem(faults.BY_NAME, "noop", noop)
    info = ProjectInfo(root=tmp_path, profile="p", seed_paths=["seeds"])
    cfg = DrillConfig(seed="orders", roles={}, faults=["noop"])
    r = run_one(info, cfg, "noop", tmp_path / "work", dbt_bin="dbt-not-called")
    assert r.outcome == NOT_APPLICABLE and "unchanged" in r.detail


def test_crashing_fault_is_not_applicable_not_an_abort(tmp_path, monkeypatch):
    from dbt_fault_drill import faults
    from dbt_fault_drill.config import DrillConfig, ProjectInfo
    from dbt_fault_drill.runner import run_one
    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds" / "orders.csv").write_text("id\n1\n")

    def boom(rows, c):
        raise ValueError("bad data")
    monkeypatch.setitem(faults.BY_NAME, "boom", faults.Fault("boom", (), "x", boom))
    info = ProjectInfo(root=tmp_path, profile="p", seed_paths=["seeds"])
    r = run_one(info, DrillConfig(seed="orders", roles={}, faults=["boom"]), "boom",
                tmp_path / "work")
    assert r.outcome == NOT_APPLICABLE and "ValueError: bad data" in r.detail


def test_markdown_explains_tool_errors_and_unplanted_faults():
    from dbt_fault_drill.runner import TOOL_ERROR
    rep = make_report([
        RunResult("duplicate_key", TOOL_ERROR, "x", detail="12:00 [ERROR]: Encountered an error:\n"
                  "Runtime Error in operation shop-on-run-end-0\n  audit: too many rows"),
        RunResult("null_key", NOT_APPLICABLE, detail="seed has no rows"),
    ])
    md = to_markdown(rep)
    assert "## Tool errors" in md and "Runtime Error in operation" in md
    assert "`null_key`: seed has no rows" in md
