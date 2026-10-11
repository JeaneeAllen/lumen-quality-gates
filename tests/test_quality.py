from datetime import date

import pytest

from scripts.generate_data import main as generate
from src.quality import (SPECS, Table, batch_range, build_mart, check_balance_coverage,
                         check_balance_movement, check_balance_reconciliation, check_columns,
                         check_foreign_key, check_mart_grain, check_required, check_row_count,
                         check_row_shape, check_types, check_unique, gate_passed, load_table,
                         load_tables, run_layers, run_mart_layer)

BATCH = batch_range(date(2025, 9, 30))  # 2025-09-01 to 2025-09-30


def make_table(name, columns, rows):
    return Table(name, columns, [dict(zip(columns, r)) for r in rows], list(range(2, len(rows) + 2)))


def balances(*rows):
    return make_table("daily_balances", ["account_id", "balance_date", "balance"], rows)


# --- Layer 1: schema ---

def test_load_table_missing_file(tmp_path):
    table, check = load_table(tmp_path, "accounts")
    assert table is None
    assert "not found" in check.failures[0]


def test_load_table_empty_file(tmp_path):
    (tmp_path / "accounts.csv").write_text("")
    _, check = load_table(tmp_path, "accounts")
    assert "is empty" in check.failures[0]


def test_load_table_header_only(tmp_path):
    (tmp_path / "branches.csv").write_text("branch_key,branch_name,region\n")
    _, check = load_table(tmp_path, "branches")
    assert "no data rows" in check.failures[0]


def test_check_columns_reports_missing_unexpected_and_duplicate():
    table = make_table("t", ["a", "x", "x"], [])
    failures = check_columns(table, ["a", "b"]).failures
    assert "column 'x' appears 2 times in header" in failures
    assert "missing column 'b'" in failures
    assert "unexpected column 'x'" in failures


def test_check_columns_passes_on_exact_match():
    assert check_columns(make_table("t", ["a", "b"], []), ["a", "b"]).passed


def test_check_row_shape_reports_short_and_long_rows(tmp_path):
    (tmp_path / "branches.csv").write_text("branch_key,branch_name,region\nBR-01,North\nBR-02,South,S,extra\n")
    table, _ = load_table(tmp_path, "branches")
    assert check_row_shape(table).failures == [
        "line 2: 2 fields, expected 3",
        "line 3: 4 fields, expected 3",
    ]


# --- Layer 2: fields ---

def test_check_required_flags_blank_and_whitespace():
    table = make_table("t", ["a", "b"], [("1", ""), ("  ", "2"), ("3", "4")])
    assert check_required(table, ["a", "b"]).failures == ["line 2: b is blank", "line 3: a is blank"]


@pytest.mark.parametrize("value", ["12O.50", "1e5", "NaN", "Infinity", "1,000.00", "$5", "."])
def test_check_types_rejects_bad_decimals(value):
    assert not check_types(balances(("A", "2025-09-01", value)), {"balance": "decimal"}).passed


@pytest.mark.parametrize("value", ["2025-13-01", "2025-02-30", "2025/09/01", "20250901", "09-01-2025"])
def test_check_types_rejects_bad_dates(value):
    assert not check_types(balances(("A", value, "1.00")), {"balance_date": "date"}).passed


def test_check_types_accepts_valid_values_and_ignores_blanks():
    table = balances(("A", "2025-09-01", "100.25"), ("A", "2025-09-02", "-3"), ("A", "", ""))
    assert check_types(table, {"balance_date": "date", "balance": "decimal"}).passed


# --- Layer 3: keys ---

def test_check_unique_single_key_reports_all_lines():
    table = make_table("t", ["id"], [("A",), ("B",), ("A",)])
    assert check_unique(table, ["id"]).failures == ["A appears on lines 2, 4"]


def test_check_unique_composite_key():
    table = balances(("A", "2025-09-01", "1"), ("A", "2025-09-02", "1"), ("A", "2025-09-01", "2"))
    assert check_unique(table, ["account_id", "balance_date"]).failures == ["A/2025-09-01 appears on lines 2, 4"]


def test_check_foreign_key_reports_orphans():
    parent = make_table("accounts", ["account_id"], [("A",), ("B",)])
    child = balances(("A", "2025-09-01", "1"), ("Z", "2025-09-01", "1"))
    assert check_foreign_key(child, "account_id", parent, "account_id").failures == [
        "line 3: account_id 'Z' not found in accounts"
    ]


# --- Layer 4: business rules ---

def test_movement_of_exactly_300_percent_passes():
    assert check_balance_movement(balances(("A", "2025-09-01", "100.00"), ("A", "2025-09-02", "400.00"))).passed


def test_movement_just_over_300_percent_fails():
    check = check_balance_movement(balances(("A", "2025-09-01", "100.00"), ("A", "2025-09-02", "400.01")))
    assert check.failures == ["line 3: A 2025-09-02 balance 400.01 vs 100.00 on 2025-09-01 (+300.0%)"]


def test_large_drop_does_not_exceed_threshold():
    assert check_balance_movement(balances(("A", "2025-09-01", "1000.00"), ("A", "2025-09-02", "1.00"))).passed


def test_movement_compares_in_date_order_not_file_order():
    table = balances(("A", "2025-09-02", "110.00"), ("A", "2025-09-01", "100.00"), ("A", "2025-09-03", "121.00"))
    assert check_balance_movement(table).passed


def test_movement_never_compares_across_accounts():
    table = balances(("A", "2025-09-01", "100.00"), ("B", "2025-09-02", "9000.00"))
    assert check_balance_movement(table).passed


def test_movement_is_not_compared_across_a_missing_day():
    table = balances(("A", "2025-09-01", "100.00"), ("A", "2025-09-03", "1000.00"))
    assert check_balance_movement(table).passed
    accounts = make_table("accounts", ["account_id"], [("A",)])
    window = (date(2025, 9, 1), date(2025, 9, 3))
    assert check_balance_coverage(table, accounts, *window).failures == ["A: missing 1 day(s): 2025-09-02"]


# --- Layer 4: source coverage against the declared batch range ---

WINDOW = (date(2025, 9, 1), date(2025, 9, 5))


def coverage(rows, account_ids=("A",)):
    accounts = make_table("accounts", ["account_id"], [(a,) for a in account_ids])
    return check_balance_coverage(balances(*rows), accounts, *WINDOW)


def full_month(account, skip=()):
    days = [f"2025-09-0{d}" for d in range(1, 6)]
    return [(account, d, "1.00") for d in days if d not in skip]


def test_batch_range_is_30_days_ending_on_as_of():
    assert BATCH == (date(2025, 9, 1), date(2025, 9, 30))


def test_coverage_passes_when_every_day_is_present_in_any_order():
    assert coverage(list(reversed(full_month("A")))).passed


def test_coverage_missing_interior_date():
    assert coverage(full_month("A", skip=["2025-09-03"])).failures == ["A: missing 1 day(s): 2025-09-03"]


def test_coverage_missing_first_date():
    assert coverage(full_month("A", skip=["2025-09-01"])).failures == ["A: missing 1 day(s): 2025-09-01"]


def test_coverage_missing_last_date():
    assert coverage(full_month("A", skip=["2025-09-05"])).failures == ["A: missing 1 day(s): 2025-09-05"]


def test_coverage_reports_multiple_gaps_as_ranges():
    rows = full_month("A", skip=["2025-09-01", "2025-09-02", "2025-09-04"])
    assert coverage(rows).failures == ["A: missing 3 day(s): 2025-09-01 to 2025-09-02, 2025-09-04"]


def test_coverage_account_with_no_balance_rows():
    assert coverage(full_month("A"), account_ids=("A", "B")).failures == ["B: no balance rows at all"]


def test_coverage_rows_outside_batch_range():
    rows = full_month("A") + [("A", "2025-08-31", "1.00"), ("A", "2025-09-06", "1.00")]
    assert coverage(rows).failures == ["A: 2 balance row(s) outside the batch range (first on line 7)"]


def test_movement_from_zero_is_flagged():
    check = check_balance_movement(balances(("A", "2025-09-01", "0.00"), ("A", "2025-09-02", "50.00")))
    assert "moved from 0.00" in check.failures[0]


def test_zero_to_zero_is_not_flagged():
    assert check_balance_movement(balances(("A", "2025-09-01", "0.00"), ("A", "2025-09-02", "0.00"))).passed


def test_every_table_declares_its_grain():
    assert SPECS["daily_balances"]["grain"] == "one row per account_id and balance_date (end-of-day closing balance)"
    assert SPECS["daily_balances"]["key"] == ["account_id", "balance_date"]
    assert all(spec["grain"] for spec in SPECS.values())


# --- Full run against generated scenarios ---

EXPECTED = {
    #                    layer 1   layer 2   layer 3   layer 4
    "clean":             ["PASS", "PASS", "PASS", "PASS"],
    "schema_drift":      ["FAIL", "SKIPPED", "SKIPPED", "SKIPPED"],
    "bad_type":          ["PASS", "FAIL", "SKIPPED", "SKIPPED"],
    "missing_required":  ["PASS", "FAIL", "SKIPPED", "SKIPPED"],
    "duplicate_account": ["PASS", "PASS", "FAIL", "SKIPPED"],
    "orphan_account":    ["PASS", "PASS", "FAIL", "SKIPPED"],
    "orphan_customer":   ["PASS", "PASS", "FAIL", "SKIPPED"],
    "duplicate_branch":  ["PASS", "PASS", "FAIL", "SKIPPED"],
    "balance_spike":     ["PASS", "PASS", "PASS", "FAIL"],
    "missing_day":       ["PASS", "PASS", "PASS", "FAIL"],
}
# invalid_status and stale_snapshot are not listed: allowed values and freshness belong to
# the accounts contract (tests/test_contract.py).


@pytest.mark.parametrize("scenario", EXPECTED)
def test_layer_statuses_per_scenario(tmp_path, scenario):
    generate(["--out", str(tmp_path), "--scenario", scenario])
    layers = run_layers(tmp_path, *BATCH)
    assert [layer.status for layer in layers] == EXPECTED[scenario]
    assert gate_passed(layers) == (scenario == "clean")


def failures(layers):
    return [f for layer in layers for check in layer.checks for f in check.failures]


def test_duplicate_account_reports_both_lines(tmp_path):
    generate(["--out", str(tmp_path), "--scenario", "duplicate_account"])
    assert failures(run_layers(tmp_path, *BATCH)) == ["ACC-00010 appears on lines 11, 202"]


def test_duplicate_branch_is_rejected_not_deduplicated(tmp_path):
    generate(["--out", str(tmp_path), "--scenario", "duplicate_branch"])
    before = (tmp_path / "branches.csv").read_bytes()
    layers = run_layers(tmp_path, *BATCH)
    assert failures(layers) == ["BR-03 appears on lines 4, 7"]
    assert not gate_passed(layers)
    assert (tmp_path / "branches.csv").read_bytes() == before
    table, _ = load_table(tmp_path, "branches")
    assert len(table.rows) == 6


def test_orphan_customer_reports_line(tmp_path):
    generate(["--out", str(tmp_path), "--scenario", "orphan_customer"])
    assert failures(run_layers(tmp_path, *BATCH)) == ["line 21: customer_id 'CUS-99999' not found in customers"]


def test_balance_spike_reports_exactly_one_line(tmp_path):
    generate(["--out", str(tmp_path), "--scenario", "balance_spike"])
    found = failures(run_layers(tmp_path, *BATCH))
    assert len(found) == 1
    assert found[0].startswith("line 226: ACC-00008 2025-09-15")
    assert "(+400.0%)" in found[0]


def test_run_layers_does_not_modify_input_files(tmp_path):
    generate(["--out", str(tmp_path), "--scenario", "duplicate_account"])
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.csv")}
    run_layers(tmp_path, *BATCH)
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.csv")} == before


def test_missing_day_scenario_reports_account_and_date(tmp_path):
    generate(["--out", str(tmp_path), "--scenario", "missing_day"])
    assert failures(run_layers(tmp_path, *BATCH)) == ["ACC-00012: missing 1 day(s): 2025-09-10"]


def test_later_batch_reports_missing_days_and_out_of_range_rows(tmp_path):
    generate(["--out", str(tmp_path)])
    found = failures(run_layers(tmp_path, *batch_range(date(2025, 10, 15))))
    assert found[:2] == ["ACC-00001: missing 15 day(s): 2025-10-01 to 2025-10-15",
                         "ACC-00001: 15 balance row(s) outside the batch range (first on line 2)"]
    assert len(found) == 400


# --- Layer 5: deposits mart ---

def clean_tables(tmp_path, scenario="clean"):
    generate(["--out", str(tmp_path), "--scenario", scenario])
    return load_tables(tmp_path)


def test_clean_mart_passes_every_layer_5_check(tmp_path):
    layer, mart = run_mart_layer(clean_tables(tmp_path))
    assert layer.status == "PASS"
    assert [c.name for c in layer.checks] == [
        "before join: accounts: unique account_id",
        "before join: customers: unique customer_id",
        "before join: branches: unique branch_key",
        "after join: mart grain is one row per account_id and balance_date",
        "after join: row count preserved (daily_balances -> deposits_mart)",
        "after join: balance totals reconcile (daily_balances -> deposits_mart)",
    ]
    assert len(mart.rows) == 6000
    assert mart.rows[0]["branch_name"] and mart.rows[0]["customer_name"]


def test_duplicate_join_key_stops_the_join(tmp_path):
    layer, mart = run_mart_layer(clean_tables(tmp_path, "duplicate_branch"))
    assert mart is None
    assert layer.status == "FAIL"
    assert [f for c in layer.checks for f in c.failures] == ["BR-03 appears on lines 4, 7"]


def test_forced_join_with_duplicate_key_is_caught_after_join(tmp_path):
    # Bypass the pre-join check to show what the post-join checks catch on their own.
    tables = clean_tables(tmp_path, "duplicate_branch")
    mart = build_mart(tables)
    source = tables["daily_balances"]
    assert len(mart.rows) > len(source.rows)  # rows were multiplied, not deduplicated
    assert not check_mart_grain(mart).passed
    assert not check_row_count(source, mart).passed
    assert not check_balance_reconciliation(source, mart).passed


def test_forced_join_with_orphan_loses_rows_and_is_caught(tmp_path):
    tables = clean_tables(tmp_path, "orphan_account")
    mart = build_mart(tables)
    source = tables["daily_balances"]
    assert check_row_count(source, mart).failures == [
        "daily_balances has 6001 rows; deposits_mart has 6000 (difference -1)"]
    grand_total = check_balance_reconciliation(source, mart).failures[0]
    assert grand_total.startswith("grand total: daily_balances ")
    assert grand_total.endswith("(difference -500.00)")


def test_reconciliation_is_decimal_exact():
    source = balances(*[("A", "2025-09-01", "0.10")] * 3)
    mart = make_table("deposits_mart", ["account_id", "balance_date", "balance"], [("A", "2025-09-01", "0.10")] * 3)
    assert sum(float(r["balance"]) for r in source.rows) != 0.3  # float would drift
    assert check_balance_reconciliation(source, mart).passed
    mart.rows[0]["balance"] = "0.11"
    assert check_balance_reconciliation(source, mart).failures == [
        "grand total: daily_balances 0.30, deposits_mart 0.31 (difference 0.01)",
        "2025-09-01: daily_balances 0.30, deposits_mart 0.31 (difference 0.01)",
    ]


def test_coverage_and_reconciliation_are_independent(tmp_path):
    # A source missing a day still joins perfectly: reconciliation cannot see missing source data.
    tables = clean_tables(tmp_path, "missing_day")
    layer, _ = run_mart_layer(tables)
    assert layer.status == "PASS"
    assert not gate_passed(run_layers(tmp_path, *BATCH))
