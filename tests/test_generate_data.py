import csv

import pytest

from scripts.generate_data import (ACCOUNT_COLUMNS, BALANCE_COLUMNS, BRANCH_COLUMNS,
                                   CUSTOMER_COLUMNS, SCENARIOS, main)

TABLES = ("branches.csv", "customers.csv", "accounts.csv", "daily_balances.csv")


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def run(tmp_path, name, *extra):
    out = tmp_path / name
    assert main(["--out", str(out), *extra]) == 0
    return out


def test_same_seed_produces_identical_files(tmp_path):
    a = run(tmp_path, "a", "--seed", "42")
    b = run(tmp_path, "b", "--seed", "42")
    for name in TABLES:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_different_seed_produces_different_files(tmp_path):
    a = run(tmp_path, "a", "--seed", "42")
    b = run(tmp_path, "b", "--seed", "7")
    assert (a / "daily_balances.csv").read_bytes() != (b / "daily_balances.csv").read_bytes()


def test_clean_headers_and_row_counts(tmp_path):
    out = run(tmp_path, "clean", "--accounts", "50", "--customers", "30", "--days", "25")
    branch_cols, branches = read_csv(out / "branches.csv")
    cust_cols, customers = read_csv(out / "customers.csv")
    acc_cols, accounts = read_csv(out / "accounts.csv")
    bal_cols, balances = read_csv(out / "daily_balances.csv")
    assert branch_cols == BRANCH_COLUMNS
    assert cust_cols == CUSTOMER_COLUMNS
    assert acc_cols == ACCOUNT_COLUMNS
    assert bal_cols == BALANCE_COLUMNS
    assert len(branches) == 5
    assert len(customers) == 30
    assert len(accounts) == 50
    assert len(balances) == 50 * 25


def test_clean_statuses_are_only_active_or_closed(tmp_path):
    out = run(tmp_path, "clean")
    _, accounts = read_csv(out / "accounts.csv")
    assert {a["status"] for a in accounts} == {"active", "closed"}


def test_clean_data_is_internally_consistent(tmp_path):
    out = run(tmp_path, "clean")
    _, branches = read_csv(out / "branches.csv")
    _, customers = read_csv(out / "customers.csv")
    _, accounts = read_csv(out / "accounts.csv")
    _, balances = read_csv(out / "daily_balances.csv")
    branch_keys = [b["branch_key"] for b in branches]
    customer_ids = [c["customer_id"] for c in customers]
    ids = [a["account_id"] for a in accounts]
    assert len(branch_keys) == len(set(branch_keys))
    assert len(customer_ids) == len(set(customer_ids))
    assert len(ids) == len(set(ids))
    assert all(a["customer_id"] in customer_ids for a in accounts)
    assert all(a["branch_key"] in branch_keys for a in accounts)
    assert {b["account_id"] for b in balances} == set(ids)
    pairs = [(b["account_id"], b["balance_date"]) for b in balances]
    assert len(pairs) == len(set(pairs))


def test_clean_snapshot_date_is_last_balance_date(tmp_path):
    out = run(tmp_path, "clean")
    _, accounts = read_csv(out / "accounts.csv")
    _, balances = read_csv(out / "daily_balances.csv")
    assert {a["snapshot_date"] for a in accounts} == {max(b["balance_date"] for b in balances)} == {"2025-09-30"}


def test_some_customers_hold_multiple_accounts(tmp_path):
    out = run(tmp_path, "clean")
    _, accounts = read_csv(out / "accounts.csv")
    customers = [a["customer_id"] for a in accounts]
    assert len(set(customers)) < len(customers)


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s != "clean"])
def test_each_defect_scenario_differs_from_clean(tmp_path, scenario):
    clean = run(tmp_path, "clean")
    defect = run(tmp_path, scenario, "--scenario", scenario)
    changed = [n for n in TABLES if (clean / n).read_bytes() != (defect / n).read_bytes()]
    assert len(changed) == 1


def test_defect_scenarios_other_than_missing_day_never_remove_rows(tmp_path):
    clean = run(tmp_path, "clean")
    clean_counts = {n: len(read_csv(clean / n)[1]) for n in TABLES}
    for scenario in SCENARIOS[1:]:
        if scenario == "missing_day":
            continue
        out = run(tmp_path, scenario, "--scenario", scenario)
        for n in TABLES:
            assert len(read_csv(out / n)[1]) >= clean_counts[n]


def test_missing_day_deletes_exactly_one_interior_balance(tmp_path):
    clean = run(tmp_path, "clean")
    out = run(tmp_path, "missing", "--scenario", "missing_day")
    _, clean_rows = read_csv(clean / "daily_balances.csv")
    _, rows = read_csv(out / "daily_balances.csv")
    removed = [r for r in clean_rows if r not in rows]
    assert len(rows) == len(clean_rows) - 1
    assert removed == [{"account_id": "ACC-00012", "balance_date": "2025-09-10", "balance": removed[0]["balance"]}]


def test_frozen_appears_only_in_invalid_status(tmp_path):
    for scenario in SCENARIOS:
        out = run(tmp_path, scenario, "--scenario", scenario)
        _, accounts = read_csv(out / "accounts.csv")
        has_frozen = any(a.get("status") == "frozen" for a in accounts)
        assert has_frozen == (scenario == "invalid_status")


def test_schema_drift_renames_column(tmp_path):
    out = run(tmp_path, "drift", "--scenario", "schema_drift")
    acc_cols, _ = read_csv(out / "accounts.csv")
    assert "account_type" not in acc_cols
    assert "acct_type" in acc_cols


def test_rejects_too_few_rows(tmp_path):
    with pytest.raises(SystemExit):
        main(["--out", str(tmp_path / "x"), "--accounts", "5"])


def test_duplicate_branch_appends_a_second_branch_key(tmp_path):
    out = run(tmp_path, "dup", "--scenario", "duplicate_branch")
    _, branches = read_csv(out / "branches.csv")
    keys = [b["branch_key"] for b in branches]
    assert len(keys) == 6
    assert keys.count("BR-03") == 2


def test_orphan_customer_references_missing_customer(tmp_path):
    out = run(tmp_path, "orphan", "--scenario", "orphan_customer")
    _, customers = read_csv(out / "customers.csv")
    _, accounts = read_csv(out / "accounts.csv")
    known = {c["customer_id"] for c in customers}
    assert [a["customer_id"] for a in accounts if a["customer_id"] not in known] == ["CUS-99999"]


def test_default_as_of_output_is_unchanged_by_explicit_flag(tmp_path):
    a = run(tmp_path, "a")
    b = run(tmp_path, "b", "--as-of", "2025-09-30")
    for name in TABLES:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_as_of_sets_last_balance_date_and_snapshot_date(tmp_path):
    out = run(tmp_path, "later", "--as-of", "2026-03-31")
    _, accounts = read_csv(out / "accounts.csv")
    _, balances = read_csv(out / "daily_balances.csv")
    dates = sorted({b["balance_date"] for b in balances})
    assert (dates[0], dates[-1], len(dates)) == ("2026-03-02", "2026-03-31", 30)
    assert {a["snapshot_date"] for a in accounts} == {"2026-03-31"}
    assert max(a["opened_date"] for a in accounts) < "2026-03-02"


def test_rejects_bad_as_of(tmp_path):
    with pytest.raises(SystemExit):
        main(["--out", str(tmp_path / "x"), "--as-of", "2025-13-01"])
