# STATUS_REPORT — dbt-fault-drill 0.1.0 (2026-10-03)

## Built
- `src/dbt_fault_drill/`: fault catalog (17 faults), config reader, runner (copy project → plant fault in seed → `dbt build` on a generated DuckDB profile → classify from `run_results.json`), Markdown/JSON report with optional impact column, CLI (`run`, `list`).
- `examples/shop/`: 240 orders / 60 customers (deterministic `make_seeds.py`), staging + mart models, basic suite (untagged) and contract suite (`tag:contract`), three in-project generic tests.
- `tests/`: 86 tests (unit + end-to-end against the example). `.github/workflows/ci.yml` (not run yet; repo not pushed).
- `reports/basic.md|json`, `reports/contract.md|json`: the numbers in the README come from these files.

## Commands run and results
- `pytest -q` → `86 passed in 70.26s` (system Python 3.11.15, dbt-core 1.12.5, dbt-duckdb 1.11.0)
- Clean venv: `python3 -m venv … && pip install -e ".[dev]" && pytest -q` → `86 passed in 71.81s`
- `dbt-fault-drill run --project-dir examples/shop --exclude tag:contract …` → `stopped 3/17, silent 14`
- `dbt-fault-drill run --project-dir examples/shop …` → `stopped 14/17, silent 3` (silent: replayed_row_new_key, amount_off_by_10x, stale_batch)

## Known limits
- Seeds + DuckDB only. Warehouse sources need a sample exported as a seed.
- The example contract and the catalog have the same author, so 14/17 is not an independent benchmark (stated in README).
- Impact = one SUM; redistribution between groups shows as 0.00 (stated in report and README).
- Example schema uses dbt ≥ 1.10 `arguments:` syntax. Not tested on dbt < 1.12 or on Python 3.10 (CI matrix covers 3.10 once pushed).

## Not done (needs 지곤님)
- GitHub repo creation and push (the README install line points at github.com/jigonyoo/dbt-fault-drill, which does not exist until then).
- PyPI upload. Site Work-section entry.
- Independent review by Codex (prompt handed over separately).

## Independent review (Codex, REVIEW_codex_20261003.md) — fixes, 2026-10-03
All nine findings fixed, each with a regression test (real dbt runs where the finding involved dbt):
1. Blocking: dbt's exit code was ignored; a failing `on-run-end` hook passed the baseline. → `classify(results, returncode)`; non-zero exit with no failing node = `tool_error`; baseline must exit 0.
2. `replayed_row_new_key` string keys could collide with an existing key. → suffix loops until unused.
3. Seed outside `--select` counted as planted/missed. → config error if the seed did not run in the baseline.
4. UTF-8 BOM broke the first column name. → read as utf-8-sig, BOM preserved on write; mapped role columns are checked up front (config error if missing).
5. Impact blank for aliased models. → relation looked up in manifest.json (schema + alias); unknown impact model is a config error.
6. `--fail-under` passed when nothing could be planted. → exit 5 "nothing was measured".
7. Empty seed: empty/truncated batch counted as planted. → not applicable; plus a guard: a fault that leaves data unchanged is never counted.
8. Tests did not catch mutations of the return code and the custom-schema lookup. → both mutations re-applied in a scratch copy and confirmed to fail the new tests.
9. Reused `--keep` path gave a traceback. → clean error, exit 2.

Results after the fixes:
- `pytest -q` → `99 passed in 174.82s`
- Example reports regenerated; every fault's outcome, impact and description identical to the previous run (basic 3/17, contract 14/17).

## Second independent review (2026-10-04) — fixes
Reviewer (separate agent, given only the repo, the earlier findings and commands) reproduced the README numbers exactly and reported:
- Blocking: `logs`/`target`/`venv` folders at any depth were left out of every copy (`models/logs/` vanished with its tests). → ignored at the project root only.
- I1: a hook that failed at run time, in fault runs only, counted as "stopped by a load error". → `operation.` nodes are tool errors.
- I2: the unchanged-data safety net compared text ("0.00" vs "0") and was untested; small amounts rounded. → amount faults use exact Decimal arithmetic and skip zero, blank and non-numeric amounts; safety net tested.
- I3: `--keep` inside the project recursed; `--keep` on a file crashed. → both are config errors.
- I4: the custom-schema lookup could break without a failing test. → decoy-relation test.
- I5: `--select` without parents failed the baseline without explanation. → hint printed, README explains.
- I6: a non-numeric amount aborted the drill with exit 1. → fault reported as not planted; unexpected errors exit 6.
- Minor: standing warnings no longer stop the drill (recorded, ignored when judging faults); truncated note states the real share; integer category codes get an unused integer; key parsing for `--5` and ` 5`; tool errors explained in the Markdown; `--target drill` so `DBT_TARGET` cannot leak in; versioned impact models use the latest version; example uses the `arguments:` syntax everywhere; README reproduce commands include `--title`.

Results after the fixes:
- `pytest -q` → `121 passed in 257.76s`
- The reviewer's three mutations (schema lookup, safety net, root-only ignore) re-applied in scratch copies: each fails at least one test.
- Example reports regenerated: every fault's outcome, impact, planted text and fired tests identical to the previous run (basic 3/17, contract 14/17). The dbt deprecation warning no longer appears.
