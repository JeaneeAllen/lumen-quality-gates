import copy
from datetime import date
from pathlib import Path

import pytest
import yaml

from scripts.generate_data import STATUSES, main as generate
from scripts.validate_contract import (ContractError, check_change_policy, check_freshness,
                                       compare_contracts, contract_problems, load_contract, main,
                                       validate_data)
from src.quality import SPECS, Table

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contracts" / "accounts.yaml"

# Evaluation date for the historical demo: the last balance date in the synthetic data.
AS_OF = date(2025, 9, 30)


@pytest.fixture
def contract():
    return load_contract(CONTRACT_PATH)


def statuses(phases):
    return [p.status for p in phases]


def failures(phases):
    return [f for p in phases for c in p.checks for f in c.failures]


# --- The contract file itself ---

def test_repository_contract_is_valid(contract):
    assert contract_problems(contract) == []


def test_contract_fields_match_quality_layer_columns(contract):
    assert [f["name"] for f in contract["fields"]] == SPECS["accounts"]["columns"]


def test_every_field_has_a_meaning(contract):
    assert all(f["meaning"].strip() for f in contract["fields"])


def test_contract_status_values_are_active_and_closed(contract):
    status = next(f for f in contract["fields"] if f["name"] == "status")
    assert status["allowed"] == ["active", "closed"]
    assert set(STATUSES) <= set(status["allowed"])


def test_malformed_contract_is_rejected(tmp_path, contract):
    broken = copy.deepcopy(contract)
    broken["fields"][0]["type"] = "integer"
    del broken["freshness"]["max_lag_days"]
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(broken))
    with pytest.raises(ContractError, match="type must be one of"):
        load_contract(path)


def test_invalid_yaml_exits_2(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("name: [unclosed")
    assert main(["--contract", str(path), "--data", str(tmp_path), "--as-of", "2025-09-30"]) == 2


# --- Data against the contract, per generated scenario ---

EXPECTED = {
    #                    structure  values     links & freshness
    "clean":             ["PASS", "PASS", "PASS"],
    "schema_drift":      ["FAIL", "SKIPPED", "SKIPPED"],
    "missing_required":  ["PASS", "FAIL", "SKIPPED"],
    "duplicate_account": ["PASS", "FAIL", "SKIPPED"],
    "invalid_status":    ["PASS", "FAIL", "SKIPPED"],
    "orphan_account":    ["PASS", "PASS", "FAIL"],
    "orphan_customer":   ["PASS", "PASS", "FAIL"],
    "stale_snapshot":    ["PASS", "PASS", "FAIL"],
    # Defects outside accounts.csv's own rules; quality layers 2-4 catch them.
    "bad_type":          ["PASS", "PASS", "PASS"],
    "balance_spike":     ["PASS", "PASS", "PASS"],
    "duplicate_branch":  ["PASS", "PASS", "PASS"],
    "missing_day":       ["PASS", "PASS", "PASS"],
}


@pytest.mark.parametrize("scenario", EXPECTED)
def test_contract_phases_per_scenario(tmp_path, contract, scenario):
    generate(["--out", str(tmp_path), "--scenario", scenario])
    assert statuses(validate_data(contract, tmp_path, AS_OF)) == EXPECTED[scenario]


def test_invalid_status_names_line_and_value(tmp_path, contract):
    generate(["--out", str(tmp_path), "--scenario", "invalid_status"])
    assert failures(validate_data(contract, tmp_path, AS_OF)) == [
        "line 16: status 'frozen' not in allowed values ['active', 'closed']"
    ]


def test_orphan_account_reports_balances_line(tmp_path, contract):
    generate(["--out", str(tmp_path), "--scenario", "orphan_account"])
    assert failures(validate_data(contract, tmp_path, AS_OF)) == [
        "line 6002: account_id 'ACC-99999' not found in accounts.csv"
    ]


def test_orphan_customer_reports_accounts_line(tmp_path, contract):
    generate(["--out", str(tmp_path), "--scenario", "orphan_customer"])
    assert failures(validate_data(contract, tmp_path, AS_OF)) == [
        "line 21: customer_id 'CUS-99999' not found in customers.csv"
    ]


def test_missing_branches_file_fails_relationship(tmp_path, contract):
    generate(["--out", str(tmp_path)])
    (tmp_path / "branches.csv").unlink()
    assert "branches.csv not found" in failures(validate_data(contract, tmp_path, AS_OF))[0]


# --- Freshness against an explicit --as-of date ---

def snapshots(*dates):
    return Table("accounts.csv", ["snapshot_date"], [{"snapshot_date": d} for d in dates],
                 list(range(2, len(dates) + 2)))


def test_freshness_all_rows_fresh(contract):
    assert check_freshness(snapshots("2025-09-30", "2025-09-30", "2025-09-29"), contract, AS_OF).passed


def test_freshness_all_rows_fresh_generated(tmp_path, contract):
    generate(["--out", str(tmp_path)])
    assert gate_ok(validate_data(contract, tmp_path, AS_OF))
    assert gate_ok(validate_data(contract, tmp_path, date(2025, 10, 1)))  # 1-day lag allowed


def test_freshness_all_rows_stale(tmp_path, contract):
    generate(["--out", str(tmp_path), "--scenario", "stale_snapshot"])
    found = failures(validate_data(contract, tmp_path, AS_OF))
    assert len(found) == 200
    assert found[0] == "line 2: snapshot_date 2025-09-23 is 7 days before --as-of 2025-09-30 (max 1)"


def test_freshness_one_fresh_row_does_not_hide_stale_rows(contract):
    check = check_freshness(snapshots("2025-09-20", "2025-09-30", "2025-09-28"), contract, AS_OF)
    assert check.failures == [
        "line 2: snapshot_date 2025-09-20 is 10 days before --as-of 2025-09-30 (max 1)",
        "line 4: snapshot_date 2025-09-28 is 2 days before --as-of 2025-09-30 (max 1)",
    ]


def test_freshness_future_snapshot_fails(contract):
    check = check_freshness(snapshots("2025-09-30", "2025-10-01"), contract, AS_OF)
    assert check.failures == ["line 3: snapshot_date 2025-10-01 is after --as-of 2025-09-30"]


def test_freshness_fails_when_accounts_and_balances_are_both_old(tmp_path, contract):
    # snapshot_date matches the last balance_date, so a data-derived date would pass.
    # Against an explicit later --as-of, both are old and the contract must fail.
    generate(["--out", str(tmp_path)])
    found = failures(validate_data(contract, tmp_path, date(2025, 10, 15)))
    assert len(found) == 200
    assert found[0] == "line 2: snapshot_date 2025-09-30 is 15 days before --as-of 2025-10-15 (max 1)"


def test_cli_requires_as_of(tmp_path):
    with pytest.raises(SystemExit):
        main(["--contract", str(CONTRACT_PATH), "--data", str(tmp_path)])


def test_cli_rejects_bad_as_of(tmp_path):
    with pytest.raises(SystemExit):
        main(["--contract", str(CONTRACT_PATH), "--data", str(tmp_path), "--as-of", "2025-13-01"])


def test_cli_exit_codes(tmp_path):
    clean, bad = tmp_path / "clean", tmp_path / "bad"
    generate(["--out", str(clean)])
    generate(["--out", str(bad), "--scenario", "invalid_status"])
    args = ["--contract", str(CONTRACT_PATH), "--as-of", "2025-09-30", "--data"]
    assert main([*args, str(clean)]) == 0
    assert main([*args, str(bad)]) == 1


def gate_ok(phases):
    return all(p.status == "PASS" for p in phases)


# --- Change policy ---

def edited(contract, version, change):
    new = copy.deepcopy(contract)
    new["version"] = version
    change(new)
    return new


def field(contract, name):
    return next(f for f in contract["fields"] if f["name"] == name)


def add_frozen(c):
    field(c, "status")["allowed"].append("frozen")


def test_unchanged_contract_passes_policy(contract):
    assert check_change_policy(contract, copy.deepcopy(contract)).passed


REJECTED = ("breaking changes are rejected by default; a version bump does not approve them "
            "(see change_policy.consumer_review_process):")


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0", "2.0.0", "10.0.0"])
def test_breaking_change_is_rejected_at_any_version(contract, version):
    check = check_change_policy(contract, edited(contract, version, add_frozen))
    assert check.failures == [REJECTED, "  - field 'status' allowed value 'frozen' added"]


@pytest.mark.parametrize("change, expected", [
    (lambda c: c["fields"].pop(), "field 'snapshot_date' removed or renamed"),
    (lambda c: field(c, "opened_date").update(type="string"), "field 'opened_date' type changed date -> string"),
    (lambda c: field(c, "customer_id").update(required=False), "field 'customer_id' required changed True -> False"),
    (lambda c: c["fields"].append({"name": "x", "type": "string", "required": True, "meaning": "x"}),
     "required field 'x' added"),
    (lambda c: field(c, "account_id").update(pattern=r"^ACC-\d+$"), "field 'account_id' pattern changed"),
    (lambda c: c["dataset"].update(primary_key=["customer_id"]), "dataset primary_key changed"),
    (lambda c: c["relationships"].pop(), "relationships changed"),
    (lambda c: c["freshness"].update(max_lag_days=30), "freshness rule changed"),
])
def test_breaking_changes_are_detected(contract, change, expected):
    breaking, _ = compare_contracts(contract, edited(contract, "1.0.0", change))
    assert expected in breaking


def test_optional_field_is_minor(contract):
    new = edited(contract, "1.1.0", lambda c: c["fields"].append(
        {"name": "nickname", "type": "string", "required": False, "meaning": "Optional label."}))
    assert compare_contracts(contract, new) == ([], ["optional field 'nickname' added"])
    assert check_change_policy(contract, new).passed


def test_meaning_change_needs_version_bump(contract):
    change = lambda c: field(c, "branch_key").update(meaning="Servicing branch.")  # noqa: E731
    assert not check_change_policy(contract, edited(contract, "1.0.0", change)).passed
    assert check_change_policy(contract, edited(contract, "1.0.1", change)).passed


def test_version_cannot_go_backwards(contract):
    newer = edited(contract, "2.0.0", lambda c: None)
    assert "version went backwards" in check_change_policy(newer, contract).failures[0]


def test_cli_baseline_blocks_unversioned_breaking_change(tmp_path, contract):
    generate(["--out", str(tmp_path / "data")])
    proposed = tmp_path / "proposed.yaml"
    proposed.write_text(yaml.safe_dump(edited(contract, "1.0.0", add_frozen)))
    args = ["--data", str(tmp_path / "data"), "--as-of", "2025-09-30", "--baseline", str(CONTRACT_PATH)]
    assert main(["--contract", str(proposed), *args]) == 1
    assert main(["--contract", str(CONTRACT_PATH), *args]) == 0


def test_cli_baseline_rejects_major_bumped_breaking_change(tmp_path, contract):
    generate(["--out", str(tmp_path / "data")])
    proposed = tmp_path / "proposed.yaml"
    proposed.write_text(yaml.safe_dump(edited(contract, "2.0.0", add_frozen)))
    assert main(["--contract", str(proposed), "--data", str(tmp_path / "data"), "--as-of", "2025-09-30",
                 "--baseline", str(CONTRACT_PATH)]) == 1
