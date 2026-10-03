# dbt-fault-drill

A batch can load without a single error and still be wrong: a duplicate key, a currency mix-up, one row with an absurd amount, last year's file loaded again.

`dbt-fault-drill` measures how many of those your dbt tests actually stop. It plants one realistic fault at a time into a copy of your seed data, runs `dbt build`, and reports which faults were stopped, which test stopped them, which went through silently, and how far each one moved a number someone reads.

Your project files are never modified. Every fault runs on its own throwaway copy with its own DuckDB file.

## What it found in the example project

`examples/shop` is a small dbt project: 240 orders, 60 customers, two staging models and a daily revenue mart. Its `schema.yml` holds two test suites. The **basic** suite is what many projects start with: `unique` and `not_null` on the keys. The **contract** suite adds relationships, accepted values, a range on amounts, a "not in the future" check on dates and a minimum row count.

| Suite | Faults stopped | Went through silently |
|---|---|---|
| basic (`unique` + `not_null` on keys) | **3 of 17** | 14 |
| contract | **14 of 17** | 3 |

What the basic suite let through, measured as the change in `SUM(revenue)` of `fct_daily_revenue` (clean baseline 20,174.75, summed across currencies as a raw check):

| Fault that went through | Change in revenue |
|---|---|
| one amount of 45,000,000 | +44,999,955.17 |
| one amount in KRW scale, currency still USD | +21,138.33 |
| one amount sent in cents | +10,790.01 |
| 90% of the batch missing | -18,207.38 |
| the whole batch empty | -20,174.75 |

The contract suite still let three through, and they are the interesting ones, because no range or uniqueness test on a single column can see them:

- **One event re-ingested under a new key** (+218.59). The surrogate keys differ, so `unique` passes.
- **One amount off by 10x** (30.95 became 309.50, +278.55). Still inside any sensible range.
- **Last year's file reloaded** (every date shifted back 365 days, revenue total unchanged). Nothing checks freshness.

These counts are for this example only, and the contract suite was written by the same person who wrote the fault catalog, so 14 of 17 is not an independent benchmark. The number that matters is the one the drill reports on your own project.

Full reports: [`reports/basic.md`](reports/basic.md) and [`reports/contract.md`](reports/contract.md). To reproduce:

```bash
pip install -e ".[duckdb]"
dbt-fault-drill run --project-dir examples/shop --exclude tag:contract --out reports/basic.md --json reports/basic.json \
  --title "examples/shop: basic suite (unique + not_null on keys)"
dbt-fault-drill run --project-dir examples/shop --out reports/contract.md --json reports/contract.json \
  --title "examples/shop: full contract suite"
```

Measured with dbt-core 1.12.5, dbt-duckdb 1.11.0, Python 3.11, fault RNG seed 7. The faults are deterministic for a given seed, so the same command gives the same table.

## Quick start

```bash
pip install "dbt-fault-drill[duckdb] @ git+https://github.com/jigonyoo/dbt-fault-drill"
```

Add `fault_drill.yml` next to your `dbt_project.yml`:

```yaml
seed: orders              # the seed (CSV) to plant faults into
roles:                    # which column plays which part; leave out what you don't have
  key: order_id
  amount: amount
  currency: currency
  date: ordered_at
  category: status
  foreign_key: customer_id
foreign_key_seed: customers        # parent seed, for the orphan-key fault
foreign_key_ref_column: customer_id
faults: all                        # or a list of names from `dbt-fault-drill list`
rng_seed: 7
impact:                            # optional: a number someone reads
  model: fct_daily_revenue
  column: revenue
```

Then:

```bash
dbt-fault-drill run --project-dir . --out drill.md --json drill.json
```

Faults whose roles you did not map are skipped and listed in the report. Before anything is planted, the drill checks that every mapped column exists, that the clean project builds with exit code 0 and no failing test (a failing `on-run-end` hook counts as a failure; warnings are allowed, recorded as standing warnings and ignored when judging the faults), that the seed is inside the dbt selection, and that the impact model can be found. If any check fails, it stops and says which one.

Useful options: `--select` / `--exclude` (passed to `dbt build`, so you can drill one suite or one part of the DAG; every run starts from an empty DuckDB file, so a selection must include every parent of what it selects, for example `+fct_daily_revenue+`), `--faults a,b` (a subset), `--jobs N` (faults in parallel, default 4), `--keep DIR` (keep the per-fault copies to inspect them), `--fail-under 80` (exit 1 if fewer than 80% of planted faults are stopped, for CI).

Exit codes: 0 done, 1 below `--fail-under`, 2 configuration or usage error, 3 the clean baseline failed, 4 a fault run failed outside dbt's seeds, models and tests (the report's "Tool errors" section says why), 5 no fault could be planted in this data, so nothing was measured, 6 an unexpected internal error.

## How a result is decided

| Result | Meaning |
|---|---|
| stopped by a test | at least one dbt test failed |
| stopped by a load error | no test failed, but a seed or model errored, so the build stopped loudly |
| warned only, still loaded | only `severity: warn` tests fired; the bad rows reached the models |
| went through silently | `dbt build` passed |
| not applicable | the fault could not be planted in this data (for example, no rows), or it would not have changed anything |
| tool error | dbt exited non-zero without a failing seed, model or test, a hook (`on-run-start`/`on-run-end`) failed, or dbt wrote no results |

The impact column is `SUM(column)` of the impact model in the faulty copy minus the same sum in the clean copy. The model is looked up in dbt's manifest, so custom schemas and aliases work. It is blank when the model was not built (dbt skips downstream models after a failing test). A change of 0.00 means the total did not move, not that the data is right: the rows can still sit in the wrong day, currency or group.

## Fault catalog

| Fault | Plants | Needs role | Stopped by |
|---|---|---|---|
| `duplicate_key` | one row delivered twice | key | `unique` on the key |
| `null_key` | one row with a blank key | key | `not_null` on the key |
| `null_amount` | one row with a blank amount | amount | `not_null` on the amount |
| `amount_in_cents` | one amount sent in cents (x100) | amount | a range test on the amount, or a batch total compared with recent batches |
| `absurd_amount` | one amount of 45,000,000 | amount | a range test on the amount |
| `negative_amount` | one amount flipped negative | amount | a lower bound on the amount (or an explicit sign rule for refunds) |
| `foreign_amount_labeled_home` | one amount in KRW scale, currency unchanged | amount | a range test per currency, or a batch total compared with recent batches |
| `unknown_currency` | one currency code not in the expected set | currency | `accepted_values` on the currency code |
| `unknown_category` | one status value never seen before | category | `accepted_values` on the status |
| `future_date` | one date in 2099 | date | a 'not in the future' test on the date |
| `date_format_drift` | one date written DD/MM/YYYY | date | a typed seed or source (the load fails) or a parse check in staging |
| `orphan_foreign_key` | one reference to a parent that does not exist | foreign_key | `relationships` to the parent model |
| `replayed_row_new_key` | one event re-ingested under a new key | key | uniqueness on a natural key or the upstream event id, not only the surrogate key |
| `amount_off_by_10x` | one amount off by a factor of 10 | amount | a per-row comparison with that customer's or product's history, or a batch total vs recent batches |
| `stale_batch` | last year's file reloaded (dates -365 days) | date | a freshness test: the newest date must be within N days of the load |
| `empty_batch` | the whole batch arrives empty | - | a minimum row count |
| `truncated_batch` | 90% of the batch is missing | - | a minimum row count, or a row count compared with recent batches |

Each fault changes one row, except the batch faults (`stale_batch`, `empty_batch`, `truncated_batch`). One bad row in an otherwise clean batch is the case that loads without an error.

## Limits

- **Seeds and DuckDB only, for now.** Faults are planted into a seed CSV and the build runs on a generated DuckDB profile. To drill a pipeline that reads from a warehouse source, export a sample of that source (masked is fine) as a seed in a scratch copy of the project.
- **One fault at a time.** Combinations are not planted.
- **The catalog is generic.** Domain rules (a refund must reference an existing charge, a price must match the price list) need faults of their own.
- **The impact number is one sum.** It shows faults that move a total; it cannot see rows that moved between groups.
- **Every fault is a full `dbt build`** of the selection, from an empty database. On a large project, select the seed and what depends on it, with its parents (`+model+`).
- **Amount faults need plain numbers.** Rows whose amount is zero, blank or not a plain number (`EUR`, `$5.00`, `12,50`) are skipped; if none are left, the fault is reported as not planted.

## Development

```bash
pip install -e ".[dev]"
pytest -q        # 121 tests; the integration tests need dbt on PATH and take about four minutes
```

The example seeds are generated by `examples/shop/make_seeds.py` (deterministic). The example's `schema.yml` uses the `arguments:` syntax for generic tests, which needs dbt-core 1.10 or newer; the drill itself only reads `target/run_results.json`.

Built with AI assistance and reviewed independently twice; every finding from both reviews is fixed with a regression test (the blocking ones: a failing `on-run-end` hook read as a pass, and nested `logs/` or `target/` folders dropped from the copies). The measurements above come from the commands shown.

## License

MIT. See [LICENSE](LICENSE).

---

Want this run against your own pipeline, with the gaps closed and the gate wired into your CI? Fixed-scope data load quality gate, from $600: [jigonyoo.com](https://jigonyoo.com)
