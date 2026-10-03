"""End-to-end runs against examples/shop. Needs dbt-core + dbt-duckdb on PATH."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from dbt_fault_drill.cli import main
from dbt_fault_drill.config import read_config, read_project
from dbt_fault_drill.runner import CAUGHT, LOAD_ERROR, MISSED, BaselineFailed, drill

pytestmark = pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")

SHOP = Path(__file__).resolve().parents[1] / "examples" / "shop"


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and "target" not in p.parts and "logs" not in p.parts:
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def run(faults, exclude=None):
    info = read_project(SHOP)
    cfg = read_config(SHOP / "fault_drill.yml")
    cfg.faults = faults
    cfg.exclude = exclude
    return {r.fault: r for r in drill(info, cfg, jobs=3).results}, cfg


def test_basic_suite_misses_what_contract_stops_and_original_is_untouched():
    before = tree_hash(SHOP)
    basic, _ = run(["duplicate_key", "absurd_amount", "orphan_foreign_key"],
                   exclude="tag:contract")
    contract, _ = run(["duplicate_key", "absurd_amount", "orphan_foreign_key"])
    assert tree_hash(SHOP) == before

    assert basic["duplicate_key"].outcome == CAUGHT
    assert basic["absurd_amount"].outcome == MISSED
    assert basic["orphan_foreign_key"].outcome == MISSED
    assert contract["absurd_amount"].outcome == CAUGHT
    assert any("value_between" in t for t in contract["absurd_amount"].failing_tests)
    assert contract["orphan_foreign_key"].outcome == CAUGHT


def test_impact_is_measured_only_when_the_mart_was_built():
    res, _ = run(["absurd_amount", "duplicate_key"], exclude="tag:contract")
    assert res["absurd_amount"].impact is not None
    assert res["absurd_amount"].impact > 40_000_000
    assert res["duplicate_key"].impact is None   # unique failed, mart skipped


def test_typed_seed_turns_bad_date_into_load_error():
    res, _ = run(["date_format_drift"])
    assert res["date_format_drift"].outcome == LOAD_ERROR


def test_contract_still_misses_plausible_faults():
    res, _ = run(["stale_batch", "replayed_row_new_key"])
    assert res["stale_batch"].outcome == MISSED
    assert res["replayed_row_new_key"].outcome == MISSED


def test_baseline_must_be_clean(tmp_path):
    proj = tmp_path / "shop"
    shutil.copytree(SHOP, proj, ignore=shutil.ignore_patterns("target", "logs", "*.duckdb"))
    seed = proj / "seeds" / "orders.csv"
    lines = seed.read_text().splitlines()
    seed.write_text("\n".join(lines + [lines[1]]) + "\n")   # pre-existing duplicate
    cfg = read_config(proj / "fault_drill.yml")
    cfg.faults = ["null_key"]
    with pytest.raises(BaselineFailed) as e:
        drill(read_project(proj), cfg)
    assert "unique_stg_orders_order_id" in e.value.result.failing_tests


def test_cli_run_writes_reports_and_fail_under(tmp_path):
    md, js = tmp_path / "r.md", tmp_path / "r.json"
    code = main(["run", "--project-dir", str(SHOP), "--exclude", "tag:contract",
                 "--faults", "duplicate_key,absurd_amount", "--out", str(md), "--json", str(js),
                 "--fail-under", "80"])
    assert code == 1                      # 1 of 2 stopped = 50% < 80%
    body = json.loads(js.read_text())
    assert body["summary"]["planted"] == 2 and body["summary"]["stopped"] == 1
    assert "went through silently" in md.read_text()


def test_cli_list(capsys):
    assert main(["list"]) == 0
    assert "replayed_row_new_key" in capsys.readouterr().out


# --- regressions from the independent review (REVIEW_codex_20261003) ---------

from dbt_fault_drill.config import ConfigError
from dbt_fault_drill.runner import NOT_APPLICABLE, TOOL_ERROR


def shop_copy(tmp_path: Path) -> Path:
    proj = tmp_path / "shop"
    shutil.copytree(SHOP, proj, ignore=shutil.ignore_patterns("target", "logs", "*.duckdb"))
    return proj


def drill_copy(proj, faults, exclude="tag:contract", select=None, **kw):
    cfg = read_config(proj / "fault_drill.yml")
    cfg.faults, cfg.exclude, cfg.select = faults, exclude, select
    return drill(read_project(proj), cfg, **kw)


def test_failing_on_run_end_hook_fails_the_baseline(tmp_path):
    proj = shop_copy(tmp_path)
    with (proj / "dbt_project.yml").open("a") as fh:
        fh.write('\non-run-end:\n  - "{% if execute %}{{ exceptions.raise_compiler_error('
                 "'hook failed') }}{% endif %}\"\n")
    with pytest.raises(BaselineFailed) as e:
        drill_copy(proj, ["absurd_amount"])
    assert e.value.result.outcome == TOOL_ERROR


def test_seed_outside_the_selection_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not part of the dbt selection"):
        drill_copy(shop_copy(tmp_path), ["duplicate_key"], select="customers")


def test_bom_seed_still_gets_its_fault(tmp_path):
    proj = shop_copy(tmp_path)
    seed = proj / "seeds" / "orders.csv"
    seed.write_bytes(b"\xef\xbb\xbf" + seed.read_bytes())
    keep = tmp_path / "kept"
    res = {r.fault: r for r in drill_copy(proj, ["duplicate_key"], keep=keep).results}
    assert res["duplicate_key"].outcome == CAUGHT
    assert (keep / "duplicate_key" / "seeds" / "orders.csv").read_bytes()[:3] == b"\xef\xbb\xbf"


@pytest.mark.parametrize("config_line", ["{{ config(alias='renamed_revenue') }}",
                                         "{{ config(schema='marts') }}"])
def test_impact_follows_alias_and_custom_schema(tmp_path, config_line):
    proj = shop_copy(tmp_path)
    mart = proj / "models" / "marts" / "fct_daily_revenue.sql"
    mart.write_text(config_line + "\n" + mart.read_text())
    rep = drill_copy(proj, ["absurd_amount"])
    assert rep.baseline.impact == pytest.approx(20174.75)
    assert rep.results[0].impact - rep.baseline.impact == pytest.approx(44999955.17)


def test_unknown_impact_model_is_a_config_error(tmp_path):
    proj = shop_copy(tmp_path)
    cfg_path = proj / "fault_drill.yml"
    cfg_path.write_text(cfg_path.read_text().replace("model: fct_daily_revenue",
                                                     "model: fct_nope"))
    with pytest.raises(ConfigError, match="impact model 'fct_nope'"):
        drill_copy(proj, ["absurd_amount"])


def test_role_pointing_at_missing_column_is_a_config_error(tmp_path):
    proj = shop_copy(tmp_path)
    cfg_path = proj / "fault_drill.yml"
    cfg_path.write_text(cfg_path.read_text().replace("key: order_id", "key: id"))
    with pytest.raises(ConfigError, match="no column for key='id'"):
        drill_copy(proj, ["duplicate_key"])


def test_empty_seed_plants_nothing_and_cli_says_so(tmp_path):
    proj = shop_copy(tmp_path)
    seed = proj / "seeds" / "orders.csv"
    seed.write_text(seed.read_text().splitlines()[0] + "\n")
    rep = drill_copy(proj, ["empty_batch", "truncated_batch", "duplicate_key"])
    assert {r.outcome for r in rep.results} == {NOT_APPLICABLE}
    code = main(["run", "--project-dir", str(proj), "--exclude", "tag:contract",
                 "--faults", "empty_batch,truncated_batch", "--fail-under", "50",
                 "--out", str(tmp_path / "r.md")])
    assert code == 5


def test_keep_directory_must_be_new_or_empty(tmp_path):
    keep = tmp_path / "kept"
    args = ["run", "--project-dir", str(SHOP), "--exclude", "tag:contract",
            "--faults", "duplicate_key", "--keep", str(keep), "--out", str(tmp_path / "r.md")]
    assert main(args) == 0
    assert main(args) == 2


# --- regressions from the second independent review --------------------------

def test_nested_logs_and_target_folders_are_project_code(tmp_path):
    proj = shop_copy(tmp_path)
    (proj / "models" / "logs").mkdir()
    (proj / "models" / "logs" / "order_log.sql").write_text(
        "select order_id, amount from {{ ref('stg_orders') }}\n")
    (proj / "models" / "logs" / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: order_log\n    columns:\n      - name: amount\n"
        "        data_tests:\n          - value_between:\n              arguments:\n"
        "                min_value: 0\n                max_value: 5000\n")
    res = drill_copy(proj, ["absurd_amount"]).results[0]
    assert res.outcome == CAUGHT
    assert any("order_log" in t for t in res.failing_tests)


def test_hook_failing_only_in_fault_runs_is_a_tool_error(tmp_path):
    proj = shop_copy(tmp_path)
    with (proj / "dbt_project.yml").open("a") as fh:
        fh.write("\non-run-end:\n  - \"select case when (select count(*) from "
                 "{{ target.schema }}.stg_orders) > 240 then error('audit: too many rows') "
                 "else 1 end\"\n")
    res = {r.fault: r for r in drill_copy(proj, ["replayed_row_new_key", "absurd_amount"]).results}
    assert res["replayed_row_new_key"].outcome == TOOL_ERROR
    assert res["absurd_amount"].outcome == MISSED


def test_keep_inside_project_or_on_a_file_is_refused(tmp_path):
    proj = shop_copy(tmp_path)
    with pytest.raises(ConfigError, match="inside the project"):
        drill_copy(proj, ["duplicate_key"], keep=proj / "drill-copies")
    afile = tmp_path / "afile"
    afile.write_text("x")
    with pytest.raises(ConfigError, match="not a directory"):
        drill_copy(proj, ["duplicate_key"], keep=afile)


def test_custom_schema_impact_ignores_a_same_named_decoy(tmp_path):
    proj = shop_copy(tmp_path)
    mart = proj / "models" / "marts" / "fct_daily_revenue.sql"
    mart.write_text("{{ config(schema='marts') }}\n" + mart.read_text())
    (proj / "models" / "marts" / "decoy.sql").write_text(
        "{{ config(alias='fct_daily_revenue') }}\nselect 1000000 as revenue\n")
    rep = drill_copy(proj, ["absurd_amount"])
    assert rep.baseline.impact == pytest.approx(20174.75)
    assert rep.results[0].impact - rep.baseline.impact == pytest.approx(44999955.17)


def test_standing_warning_on_clean_data_does_not_stop_the_drill(tmp_path):
    proj = shop_copy(tmp_path)
    schema = proj / "models" / "staging" / "schema.yml"
    schema.write_text(schema.read_text().replace(
        "                min_value: 0\n                max_value: 5000\n              config: {tags: [contract]}",
        "                min_value: 0\n                max_value: 100\n              config: {severity: warn}"))
    rep = drill_copy(proj, ["absurd_amount", "duplicate_key"])
    assert rep.standing_warnings == ["value_between_stg_orders_amount__100__0"]
    res = {r.fault: r for r in rep.results}
    assert res["absurd_amount"].outcome == MISSED          # only the standing warning fired
    assert res["duplicate_key"].outcome == CAUGHT


def test_text_amount_column_does_not_crash_the_cli(tmp_path):
    proj = shop_copy(tmp_path)
    cfg = proj / "fault_drill.yml"
    cfg.write_text(cfg.read_text().replace("  amount: amount", "  amount: currency"))
    code = main(["run", "--project-dir", str(proj), "--exclude", "tag:contract",
                 "--faults", "amount_in_cents,duplicate_key", "--json", str(tmp_path / "r.json"),
                 "--out", str(tmp_path / "r.md")])
    assert code == 0
    body = json.loads((tmp_path / "r.json").read_text())
    by = {r["fault"]: r for r in body["results"]}
    assert by["amount_in_cents"]["outcome"] == NOT_APPLICABLE


def test_select_without_parents_explains_itself(tmp_path, capsys):
    code = main(["run", "--project-dir", str(SHOP), "--select", "orders+",
                 "--faults", "duplicate_key", "--out", str(tmp_path / "r.md")])
    assert code == 3
    assert "must include every parent" in capsys.readouterr().err


def test_dbt_target_environment_variable_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("DBT_TARGET", "prod")
    res = drill_copy(shop_copy(tmp_path), ["duplicate_key"]).results[0]
    assert res.outcome == CAUGHT


def test_versioned_impact_model_uses_the_latest_version(tmp_path):
    proj = shop_copy(tmp_path)
    marts = proj / "models" / "marts"
    src = (marts / "fct_daily_revenue.sql").read_text()
    (marts / "fct_daily_revenue.sql").rename(marts / "fct_daily_revenue_v1.sql")
    (marts / "fct_daily_revenue_v2.sql").write_text(
        src.replace("sum(amount) as revenue", "sum(amount) * 2 as revenue"))
    (marts / "versions.yml").write_text(
        "version: 2\nmodels:\n  - name: fct_daily_revenue\n    latest_version: 2\n"
        "    versions:\n      - v: 1\n      - v: 2\n")
    rep = drill_copy(proj, ["duplicate_key"])
    assert rep.baseline.impact == pytest.approx(40349.50)
