"""Fault catalog.

Each fault takes the rows of one seed CSV (a list of dicts, all values as
strings, exactly as they sit in the file) and returns a modified copy plus a
one-line note saying what was planted. Faults never touch the original file;
the runner works on a throwaway copy of the project.

Faults are deliberately small and realistic: one bad row in an otherwise
clean batch, because that is the case that loads without a single error.
"""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Callable

Rows = list[dict[str, str]]


@dataclass(frozen=True)
class Context:
    """What a fault may need besides the rows it mutates."""

    roles: dict[str, str]              # role name -> column name
    known_fk_values: frozenset[str]    # values present in the referenced seed
    rng: random.Random


@dataclass(frozen=True)
class Fault:
    name: str
    needs: tuple[str, ...]             # roles that must be mapped for this fault
    summary: str
    apply: Callable[[Rows, Context], tuple[Rows, str]]
    catch_hint: str = ""               # the kind of test that stops this fault


class FaultNotApplicable(Exception):
    """Raised when a fault cannot be planted in this data (e.g. no rows)."""


def _pick(rows: Rows, ctx: Context, column: str | None = None) -> int:
    """Pick a row index, preferring rows where `column` is non-empty."""
    if not rows:
        raise FaultNotApplicable("seed has no rows")
    candidates = list(range(len(rows)))
    if column is not None:
        candidates = [i for i in candidates if rows[i].get(column, "").strip() != ""]
        if not candidates:
            raise FaultNotApplicable(f"no non-empty values in column {column!r}")
    return ctx.rng.choice(candidates)


def _num(value: str) -> Decimal:
    """Parse a plain number exactly; anything else (EUR, $5.00, 12,50) is not a number."""
    try:
        d = Decimal(value.strip())
    except InvalidOperation:
        raise FaultNotApplicable(f"not a plain number: {value!r}") from None
    if not d.is_finite():
        raise FaultNotApplicable(f"not a finite number: {value!r}")
    return d


def _fmt(d: Decimal) -> str:
    if d == d.to_integral_value():
        return str(int(d))
    return str(d)


def _pick_amount(rows: Rows, ctx: Context) -> int:
    """Pick a row whose amount is a non-zero plain number, so scaling or flipping
    it really changes the value."""
    col = ctx.roles["amount"]
    if not rows:
        raise FaultNotApplicable("seed has no rows")
    candidates = []
    for i, r in enumerate(rows):
        v = r.get(col, "").strip()
        if not v:
            continue
        try:
            if _num(v) != 0:
                candidates.append(i)
        except FaultNotApplicable:
            continue
    if not candidates:
        raise FaultNotApplicable(f"no non-zero numeric values in column {col!r}")
    return ctx.rng.choice(candidates)


# --- individual faults -----------------------------------------------------

def duplicate_key(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    key = ctx.roles["key"]
    i = _pick(rows, ctx, key)
    out = copy.deepcopy(rows)
    out.append(dict(rows[i]))
    return out, f"row with {key}={rows[i][key]} appended a second time"


def null_key(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    key = ctx.roles["key"]
    i = _pick(rows, ctx, key)
    out = copy.deepcopy(rows)
    old = out[i][key]
    out[i][key] = ""
    return out, f"{key} blanked on one row (was {old})"


def null_amount(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    col = ctx.roles["amount"]
    i = _pick(rows, ctx, col)
    out = copy.deepcopy(rows)
    old = out[i][col]
    out[i][col] = ""
    return out, f"{col} blanked on one row (was {old})"


def amount_in_cents(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    """One row sent in minor units (cents) instead of major units."""
    col = ctx.roles["amount"]
    i = _pick_amount(rows, ctx)
    out = copy.deepcopy(rows)
    old = out[i][col]
    out[i][col] = _fmt(_num(old) * 100)
    return out, f"{col} {old} -> {out[i][col]} on one row (x100, cents as dollars)"


def absurd_amount(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    col = ctx.roles["amount"]
    i = _pick_amount(rows, ctx)
    out = copy.deepcopy(rows)
    old = out[i][col]
    out[i][col] = "45000000"
    return out, f"{col} {old} -> 45000000 on one row"


def negative_amount(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    col = ctx.roles["amount"]
    i = _pick_amount(rows, ctx)
    out = copy.deepcopy(rows)
    old = out[i][col]
    out[i][col] = _fmt(-abs(_num(old)))
    return out, f"{col} {old} -> {out[i][col]} on one row"


def foreign_amount_labeled_home(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    """Amount converted to KRW scale while the currency code still says USD/EUR."""
    col = ctx.roles["amount"]
    i = _pick_amount(rows, ctx)
    out = copy.deepcopy(rows)
    old = out[i][col]
    out[i][col] = _fmt((_num(old) * 1350).to_integral_value(rounding=ROUND_HALF_EVEN))
    cur = ctx.roles.get("currency")
    label = f" (currency still {out[i][cur]})" if cur else ""
    return out, f"{col} {old} -> {out[i][col]} on one row, KRW scale{label}"


def unknown_currency(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    col = ctx.roles["currency"]
    i = _pick(rows, ctx, col)
    out = copy.deepcopy(rows)
    old = out[i][col]
    out[i][col] = old.lower() if old.lower() != old else "XXX"
    return out, f"{col} {old!r} -> {out[i][col]!r} on one row"


def unknown_category(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    """A category value the downstream logic has never seen (case drift)."""
    col = ctx.roles["category"]
    i = _pick(rows, ctx, col)
    out = copy.deepcopy(rows)
    old = out[i][col]
    values = [r.get(col, "").strip() for r in rows if r.get(col, "").strip()]
    if all(v.lstrip("-").isdigit() for v in values):
        new = str(max(int(v) for v in values) + 1)      # an integer code nobody uses yet
    else:
        new = old.upper() if old.upper() != old else old + "_v2"
    out[i][col] = new
    return out, f"{col} {old!r} -> {new!r} on one row"


def future_date(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    col = ctx.roles["date"]
    i = _pick(rows, ctx, col)
    out = copy.deepcopy(rows)
    old = out[i][col]
    out[i][col] = "2099-01-01"
    return out, f"{col} {old} -> 2099-01-01 on one row"


def date_format_drift(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    """One row written as DD/MM/YYYY instead of ISO."""
    col = ctx.roles["date"]
    i = _pick(rows, ctx, col)
    out = copy.deepcopy(rows)
    old = out[i][col]
    parts = old[:10].split("-")
    if len(parts) != 3:
        raise FaultNotApplicable(f"{col} is not ISO formatted: {old!r}")
    y, m, d = parts
    out[i][col] = f"{d}/{m}/{y}"
    return out, f"{col} {old} -> {out[i][col]} on one row"


def orphan_foreign_key(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    col = ctx.roles["foreign_key"]
    i = _pick(rows, ctx, col)
    out = copy.deepcopy(rows)
    old = out[i][col]
    candidate = 999999
    known = ctx.known_fk_values
    while str(candidate) in known:
        candidate += 1
    out[i][col] = str(candidate)
    return out, f"{col} {old} -> {candidate} on one row (no such parent)"


def replayed_row_new_key(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    """The same event ingested twice, the second time under a freshly minted key.

    A uniqueness test on the key cannot see this: the keys differ.
    """
    key = ctx.roles["key"]
    i = _pick(rows, ctx, key)
    out = copy.deepcopy(rows)
    dup = dict(rows[i])
    keys = [r[key] for r in rows if r.get(key, "").strip()]

    def as_int(k: str):
        try:
            return int(k.strip())
        except ValueError:
            return None

    numbers = [as_int(k) for k in keys]
    if all(n is not None for n in numbers):
        dup[key] = str(max(numbers) + 1)
    else:
        taken = set(keys)
        n, candidate = 1, rows[i][key] + "-replay"
        while candidate in taken:
            n += 1
            candidate = f"{rows[i][key]}-replay{n}"
        dup[key] = candidate
    out.append(dup)
    return out, f"row {key}={rows[i][key]} re-ingested as {key}={dup[key]} (same content, new key)"


def amount_off_by_10x(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    """A plausible-looking wrong amount: one decimal place slipped."""
    col = ctx.roles["amount"]
    i = _pick_amount(rows, ctx)
    out = copy.deepcopy(rows)
    old = out[i][col]
    out[i][col] = _fmt(_num(old) * 10)
    return out, f"{col} {old} -> {out[i][col]} on one row (x10)"


def stale_batch(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    """Last year's export reloaded: every date shifted back 365 days."""
    from datetime import date, timedelta

    col = ctx.roles["date"]
    out = copy.deepcopy(rows)
    shifted = 0
    for r in out:
        v = r.get(col, "")
        try:
            d = date.fromisoformat(v[:10])
        except ValueError:
            continue
        r[col] = (d - timedelta(days=365)).isoformat() + v[10:]
        shifted += 1
    if not shifted:
        raise FaultNotApplicable(f"no ISO dates in {col!r}")
    return out, f"all {shifted} {col} values shifted back 365 days (old file reloaded)"


def empty_batch(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    if not rows:
        raise FaultNotApplicable("seed is already empty")
    return [], f"all {len(rows)} rows removed (header only)"


def truncated_batch(rows: Rows, ctx: Context) -> tuple[Rows, str]:
    keep = max(1, len(rows) // 10)
    if keep >= len(rows):
        raise FaultNotApplicable(f"only {len(rows)} row(s); nothing to truncate")
    missing = round(100 * (len(rows) - keep) / len(rows))
    return copy.deepcopy(rows[:keep]), f"{len(rows)} rows -> {keep} rows ({missing}% missing)"


CATALOG: tuple[Fault, ...] = (
    Fault("duplicate_key", ("key",), "one row delivered twice", duplicate_key,
          catch_hint='`unique` on the key'),
    Fault("null_key", ("key",), "one row with a blank key", null_key,
          catch_hint='`not_null` on the key'),
    Fault("null_amount", ("amount",), "one row with a blank amount", null_amount,
          catch_hint='`not_null` on the amount'),
    Fault("amount_in_cents", ("amount",), "one amount sent in cents (x100)", amount_in_cents,
          catch_hint='a range test on the amount, or a batch total compared with recent batches'),
    Fault("absurd_amount", ("amount",), "one amount of 45,000,000", absurd_amount,
          catch_hint='a range test on the amount'),
    Fault("negative_amount", ("amount",), "one amount flipped negative", negative_amount,
          catch_hint='a lower bound on the amount (or an explicit sign rule for refunds)'),
    Fault("foreign_amount_labeled_home", ("amount",), "one amount in KRW scale, currency unchanged",
          foreign_amount_labeled_home,
          catch_hint='a range test per currency, or a batch total compared with recent batches'),
    Fault("unknown_currency", ("currency",), "one currency code not in the expected set", unknown_currency,
          catch_hint='`accepted_values` on the currency code'),
    Fault("unknown_category", ("category",), "one status value never seen before", unknown_category,
          catch_hint='`accepted_values` on the status'),
    Fault("future_date", ("date",), "one date in 2099", future_date,
          catch_hint="a 'not in the future' test on the date"),
    Fault("date_format_drift", ("date",), "one date written DD/MM/YYYY", date_format_drift,
          catch_hint='a typed seed or source (the load fails) or a parse check in staging'),
    Fault("orphan_foreign_key", ("foreign_key",), "one reference to a parent that does not exist",
          orphan_foreign_key,
          catch_hint='`relationships` to the parent model'),
    Fault("replayed_row_new_key", ("key",), "one event re-ingested under a new key",
          replayed_row_new_key,
          catch_hint='uniqueness on a natural key or the upstream event id, not only the surrogate key'),
    Fault("amount_off_by_10x", ("amount",), "one amount off by a factor of 10", amount_off_by_10x,
          catch_hint="a per-row comparison with that customer's or product's history, or a batch total vs recent batches"),
    Fault("stale_batch", ("date",), "last year's file reloaded (dates -365 days)", stale_batch,
          catch_hint='a freshness test: the newest date must be within N days of the load'),
    Fault("empty_batch", (), "the whole batch arrives empty", empty_batch,
          catch_hint='a minimum row count'),
    Fault("truncated_batch", (), "90% of the batch is missing", truncated_batch,
          catch_hint='a minimum row count, or a row count compared with recent batches'),
)

BY_NAME = {f.name: f for f in CATALOG}
