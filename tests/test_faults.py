import copy
import random

import pytest

from dbt_fault_drill.faults import BY_NAME, CATALOG, Context, FaultNotApplicable

ROWS = [
    {"order_id": "1", "customer_id": "10", "ordered_at": "2026-09-01", "status": "placed",
     "currency": "USD", "amount": "12.50"},
    {"order_id": "2", "customer_id": "11", "ordered_at": "2026-09-02", "status": "shipped",
     "currency": "EUR", "amount": "40.00"},
    {"order_id": "3", "customer_id": "10", "ordered_at": "2026-09-03", "status": "delivered",
     "currency": "USD", "amount": "7"},
]
ROLES = {"key": "order_id", "amount": "amount", "currency": "currency", "date": "ordered_at",
         "category": "status", "foreign_key": "customer_id"}


def ctx(seed="t"):
    return Context(roles=ROLES, known_fk_values=frozenset({"10", "11"}), rng=random.Random(seed))


def changed_rows(before, after):
    return [(b, a) for b, a in zip(before, after) if b != a]


@pytest.mark.parametrize("fault", [f.name for f in CATALOG])
def test_every_fault_changes_data_and_leaves_input_alone(fault):
    original = copy.deepcopy(ROWS)
    out, note = BY_NAME[fault].apply(ROWS, ctx())
    assert ROWS == original, "fault mutated its input"
    assert out != ROWS
    assert note


@pytest.mark.parametrize("fault", [f.name for f in CATALOG])
def test_every_fault_is_deterministic_for_a_seed(fault):
    a = BY_NAME[fault].apply(ROWS, ctx("same"))
    b = BY_NAME[fault].apply(ROWS, ctx("same"))
    assert a == b


@pytest.mark.parametrize("fault", [f.name for f in CATALOG if f.name not in
                                   ("empty_batch", "truncated_batch")])
def test_row_faults_refuse_empty_input(fault):
    with pytest.raises(FaultNotApplicable):
        BY_NAME[fault].apply([], ctx())


def test_every_fault_has_a_catch_hint():
    assert all(f.catch_hint for f in CATALOG)


def test_duplicate_key_appends_exact_copy():
    out, _ = BY_NAME["duplicate_key"].apply(ROWS, ctx())
    assert len(out) == 4 and out[-1] in ROWS


def test_replayed_row_gets_new_unused_key_same_content():
    out, _ = BY_NAME["replayed_row_new_key"].apply(ROWS, ctx())
    new = out[-1]
    assert new["order_id"] == "4"
    twin = [r for r in ROWS if {k: v for k, v in r.items() if k != "order_id"}
            == {k: v for k, v in new.items() if k != "order_id"}]
    assert len(twin) == 1


def test_amount_faults_scale_one_row():
    for name, factor in (("amount_in_cents", 100), ("amount_off_by_10x", 10)):
        out, _ = BY_NAME[name].apply(ROWS, ctx())
        (b, a), = changed_rows(ROWS, out)
        assert float(a["amount"]) == pytest.approx(float(b["amount"]) * factor)


def test_negative_amount():
    out, _ = BY_NAME["negative_amount"].apply(ROWS, ctx())
    (b, a), = changed_rows(ROWS, out)
    assert float(a["amount"]) == -float(b["amount"])


def test_orphan_key_not_in_parent():
    out, _ = BY_NAME["orphan_foreign_key"].apply(ROWS, ctx())
    (_, a), = changed_rows(ROWS, out)
    assert a["customer_id"] not in {"10", "11"}


def test_date_format_drift_is_dd_mm_yyyy():
    out, _ = BY_NAME["date_format_drift"].apply(ROWS, ctx())
    (b, a), = changed_rows(ROWS, out)
    y, m, d = b["ordered_at"].split("-")
    assert a["ordered_at"] == f"{d}/{m}/{y}"


def test_date_format_drift_needs_iso():
    rows = [dict(ROWS[0], ordered_at="Sept 1")]
    with pytest.raises(FaultNotApplicable):
        BY_NAME["date_format_drift"].apply(rows, ctx())


def test_stale_batch_shifts_every_date():
    out, _ = BY_NAME["stale_batch"].apply(ROWS, ctx())
    assert [r["ordered_at"] for r in out] == ["2025-09-01", "2025-09-02", "2025-09-03"]


def test_unknown_category_and_currency_change_only_case_or_code():
    out, _ = BY_NAME["unknown_category"].apply(ROWS, ctx())
    (b, a), = changed_rows(ROWS, out)
    assert a["status"] == b["status"].upper()
    out, _ = BY_NAME["unknown_currency"].apply(ROWS, ctx())
    (b, a), = changed_rows(ROWS, out)
    assert a["currency"] == b["currency"].lower()


def test_batch_faults():
    assert BY_NAME["empty_batch"].apply(ROWS, ctx())[0] == []
    assert len(BY_NAME["truncated_batch"].apply(ROWS, ctx())[0]) == 1


def test_null_faults_skip_already_blank_values():
    rows = [dict(ROWS[0], amount=""), ROWS[1]]
    out, _ = BY_NAME["null_amount"].apply(rows, ctx())
    assert out[1]["amount"] == "" and out[0]["amount"] == ""


def test_replayed_string_key_never_reuses_an_existing_key():
    rows = [{"id": "A"}, {"id": "A-replay"}, {"id": "A-replay2"}]
    roles = {"key": "id"}
    for seed in range(50):
        c = Context(roles=roles, known_fk_values=frozenset(), rng=random.Random(seed))
        out, _ = BY_NAME["replayed_row_new_key"].apply(rows, c)
        assert out[-1]["id"] not in {r["id"] for r in rows}


def test_batch_faults_refuse_input_they_cannot_change():
    with pytest.raises(FaultNotApplicable):
        BY_NAME["empty_batch"].apply([], ctx())
    with pytest.raises(FaultNotApplicable):
        BY_NAME["truncated_batch"].apply(ROWS[:1], ctx())


# --- second independent review ---------------------------------------------

def test_amount_faults_skip_zero_and_non_numeric_rows():
    rows = [dict(ROWS[0], amount="0.00"), dict(ROWS[1], amount="EUR"), dict(ROWS[2], amount="7")]
    for name in ("negative_amount", "amount_in_cents", "amount_off_by_10x",
                 "foreign_amount_labeled_home", "absurd_amount"):
        out, _ = BY_NAME[name].apply(rows, ctx())
        (b, a), = changed_rows(rows, out)
        assert b["amount"] == "7", name


def test_amount_faults_are_exact_for_small_units():
    rows = [dict(ROWS[0], amount="0.0004")]
    assert BY_NAME["negative_amount"].apply(rows, ctx())[0][0]["amount"] == "-0.0004"
    assert BY_NAME["amount_off_by_10x"].apply(rows, ctx())[0][0]["amount"] == "0.0040"


def test_amount_faults_refuse_columns_without_numbers():
    rows = [dict(r, amount=v) for r, v in zip(ROWS, ["EUR", "12,50", "$5.00"])]
    with pytest.raises(FaultNotApplicable, match="no non-zero numeric"):
        BY_NAME["amount_in_cents"].apply(rows, ctx())


def test_truncated_note_states_the_real_share():
    rows = [ROWS[0], ROWS[1]]
    _, note = BY_NAME["truncated_batch"].apply(rows, ctx())
    assert note == "2 rows -> 1 rows (50% missing)"


def test_unknown_category_on_integer_codes_is_an_unused_integer():
    rows = [dict(ROWS[0], status="3"), dict(ROWS[1], status="7")]
    out, _ = BY_NAME["unknown_category"].apply(rows, ctx())
    assert {r["status"] for r in out} - {"3", "7"} == {"8"}


@pytest.mark.parametrize("keys, expected", [(["--5", "x"], None), ([" 5", "6"], "7"),
                                            (["-3", "-1"], "0")])
def test_replayed_key_parsing(keys, expected):
    rows = [{"id": k} for k in keys]
    c = Context(roles={"key": "id"}, known_fk_values=frozenset(), rng=random.Random(1))
    out, _ = BY_NAME["replayed_row_new_key"].apply(rows, c)
    new = out[-1]["id"]
    assert new not in keys
    if expected is not None:
        assert new == expected
