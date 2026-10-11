import csv
import hashlib
import json
from decimal import Decimal

import pytest

from scripts.build_report import NOTICE, latest_day_metrics, main as build_report
from scripts.generate_data import main as generate
from scripts.run_gate import main as run_gate
from src.quality import MART_COLUMNS

BRANCH_NAMES = {"BR-01": ("Lumen North", "Northern"), "BR-02": ("Lumen South", "Southern")}


def row(account, customer, branch, day, balance):
    name, region = BRANCH_NAMES[branch]
    return {"account_id": account, "balance_date": day, "balance": balance, "customer_id": customer,
            "customer_name": "Test Person", "segment": "retail", "account_type": "checking",
            "status": "active", "branch_key": branch, "branch_name": name, "region": region}


# Two days. On the latest day C1 holds A1 (BR-01, 100) and A2 (BR-02, 300); C2 holds A3 (BR-02, 200).
ROWS = [
    row("A1", "C1", "BR-01", "2025-09-29", "1000.00"),
    row("A2", "C1", "BR-02", "2025-09-29", "1000.00"),
    row("A3", "C2", "BR-02", "2025-09-29", "1000.00"),
    row("A1", "C1", "BR-01", "2025-09-30", "100.00"),
    row("A2", "C1", "BR-02", "2025-09-30", "300.00"),
    row("A3", "C2", "BR-02", "2025-09-30", "200.00"),
]


def write_validated_mart(mart_dir, rows=ROWS, **result_overrides):
    """Write a mart and a matching PASS gate result, as run_gate.py would."""
    mart_dir.mkdir(parents=True, exist_ok=True)
    path = mart_dir / "deposits_mart.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MART_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    result = {
        "status": "PASS", "as_of": "2025-09-30", "batch_start": "2025-09-29", "batch_end": "2025-09-30",
        "contract_version": "1.0.0",
        "mart": {"file": "deposits_mart.csv", "rows": len(rows),
                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
    }
    result.update(result_overrides)
    (mart_dir / "gate_result.json").write_text(json.dumps(result))
    return result


@pytest.fixture
def env(tmp_path):
    label = tmp_path / "release_label.txt"
    label.write_text("staging-test\n")
    mart, site = tmp_path / "mart", tmp_path / "site"

    def build():
        return build_report(["--mart-dir", str(mart), "--out", str(site), "--release-label", str(label)])
    return mart, site, label, build


# --- Metrics ---

def test_customer_totals_are_summed_before_averaging():
    latest = latest_day_metrics(ROWS)["latest_day"]
    assert latest["total_balance"] == "600.00"
    assert latest["customers"] == 2
    # C1 = 100 + 300 = 400, C2 = 200 -> (400 + 200) / 2 = 300.00.
    # A per-account average would be 600 / 3 = 200.00, which is wrong.
    assert latest["average_customer_balance"] == "300.00"


def test_only_the_latest_balance_date_is_used():
    metrics = latest_day_metrics(ROWS)
    assert metrics["latest_balance_date"] == "2025-09-30"
    assert metrics["latest_day"]["rows"] == 3
    assert metrics["latest_day"]["total_balance"] == "600.00"
    whole_batch = sum(Decimal(r["balance"]) for r in ROWS)
    assert whole_batch == Decimal("3600.00")  # what summing every snapshot would wrongly report


def test_latest_day_is_found_regardless_of_row_order():
    assert latest_day_metrics(list(reversed(ROWS)))["latest_day"]["total_balance"] == "600.00"


def test_branch_summary_splits_multi_branch_customers_by_branch():
    branches = {b["branch_key"]: b for b in latest_day_metrics(ROWS)["branches"]}
    assert branches["BR-01"] == {"branch_key": "BR-01", "branch_name": "Lumen North", "region": "Northern",
                                 "accounts": 1, "customers": 1, "total_balance": "100.00",
                                 "average_customer_balance": "100.00"}
    # C1 counts at BR-02 too, with only the BR-02 balance (300); C2 has 200 -> average 250.
    assert branches["BR-02"]["customers"] == 2
    assert branches["BR-02"]["total_balance"] == "500.00"
    assert branches["BR-02"]["average_customer_balance"] == "250.00"
    assert sum(Decimal(b["total_balance"]) for b in branches.values()) == Decimal("600.00")
    assert sum(b["customers"] for b in branches.values()) == 3  # more than the 2 distinct customers


def test_counts_are_distinct_values_in_the_mart():
    assert latest_day_metrics(ROWS)["counts"] == {"branches": 2, "customers": 2, "accounts": 3}


def test_average_rounds_half_up_to_cents():
    rows = [row("A1", "C1", "BR-01", "2025-09-30", "0.01"), row("A2", "C2", "BR-01", "2025-09-30", "0.00")]
    assert latest_day_metrics(rows)["latest_day"]["average_customer_balance"] == "0.01"  # 0.005 -> 0.01
    rows = [row(f"A{i}", f"C{i}", "BR-01", "2025-09-30", v) for i, v in enumerate(["100.00", "0.00", "0.00"])]
    assert latest_day_metrics(rows)["latest_day"]["average_customer_balance"] == "33.33"


# --- Evidence checks ---

def assert_rejected(build, site, capsys, message):
    assert build() == 1
    out = capsys.readouterr().out
    assert "REPORT REJECTED" in out and message in out
    assert not (site / "index.html").exists()
    assert not (site / "report.json").exists()


def test_missing_gate_result_is_rejected(env, capsys):
    mart, site, _, build = env
    write_validated_mart(mart)
    (mart / "gate_result.json").unlink()
    assert_rejected(build, site, capsys, "gate_result.json not found")


def test_missing_mart_is_rejected(env, capsys):
    mart, site, _, build = env
    write_validated_mart(mart)
    (mart / "deposits_mart.csv").unlink()
    assert_rejected(build, site, capsys, "deposits_mart.csv not found")


def test_failed_gate_is_rejected(env, capsys):
    mart, site, _, build = env
    write_validated_mart(mart, status="FAIL")
    assert_rejected(build, site, capsys, "gate status is 'FAIL'")


def test_malformed_gate_json_is_rejected(env, capsys):
    mart, site, _, build = env
    write_validated_mart(mart)
    (mart / "gate_result.json").write_text("{not json")
    assert_rejected(build, site, capsys, "not valid JSON")


def test_gate_result_missing_fields_is_rejected(env, capsys):
    mart, site, _, build = env
    write_validated_mart(mart)
    result = json.loads((mart / "gate_result.json").read_text())
    del result["mart"]["sha256"], result["batch_end"]
    (mart / "gate_result.json").write_text(json.dumps(result))
    assert_rejected(build, site, capsys, "missing 'batch_end'; missing 'mart.sha256'")


def test_mart_changed_after_validation_is_rejected(env, capsys):
    mart, site, _, build = env
    write_validated_mart(mart)
    path = mart / "deposits_mart.csv"
    path.write_text(path.read_text().replace("300.00", "3000.00"))
    assert_rejected(build, site, capsys, "changed after validation")


def test_latest_date_must_match_gate_batch_end(env, capsys):
    mart, site, _, build = env
    write_validated_mart(mart, batch_end="2025-10-01")
    assert_rejected(build, site, capsys, "does not match the gate's batch end")


def test_missing_release_label_is_rejected(env, capsys):
    mart, site, label, build = env
    write_validated_mart(mart)
    label.unlink()
    assert_rejected(build, site, capsys, "release_label.txt not found")


def test_failed_build_removes_previous_report(env, capsys):
    mart, site, _, build = env
    write_validated_mart(mart)
    assert build() == 0
    assert (site / "index.html").exists()
    write_validated_mart(mart, status="FAIL")
    assert_rejected(build, site, capsys, "gate status is 'FAIL'")


# --- Successful output ---

def test_successful_report_from_small_mart(env, monkeypatch):
    mart, site, _, build = env
    monkeypatch.setenv("GITHUB_SHA", "abc123def456")
    write_validated_mart(mart)
    assert build() == 0
    report = json.loads((site / "report.json").read_text())
    assert report["notice"] == NOTICE == "Lumen staging demonstration — synthetic data."
    assert report["release_label"] == "staging-test"
    assert report["source_commit"] == "abc123def456"
    assert report["latest_day"]["average_customer_balance"] == "300.00"
    page = (site / "index.html").read_text()
    for text in (NOTICE, "staging-test", "abc123def456", "2025-09-29 to 2025-09-30",
                 "$600.00", "$300.00", "$250.00", "Lumen South"):
        assert text in page


def test_end_to_end_report_from_gated_clean_data(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "e2e-commit")
    data, mart, site, label = tmp_path / "data", tmp_path / "mart", tmp_path / "site", tmp_path / "label.txt"
    label.write_text("staging-v1\n")
    generate(["--out", str(data)])
    assert run_gate(["--data", str(data), "--as-of", "2025-09-30", "--out", str(mart)]) == 0
    assert build_report(["--mart-dir", str(mart), "--out", str(site), "--release-label", str(label)]) == 0
    report = json.loads((site / "report.json").read_text())

    with open(mart / "deposits_mart.csv", newline="") as f:
        last_day = [r for r in csv.DictReader(f) if r["balance_date"] == "2025-09-30"]
    expected_total = sum(Decimal(r["balance"]) for r in last_day)
    customers = {r["customer_id"] for r in last_day}

    assert report["as_of"] == "2025-09-30"
    assert (report["batch_start"], report["batch_end"]) == ("2025-09-01", "2025-09-30")
    assert report["counts"] == {"branches": 5, "customers": 99, "accounts": 200}
    assert report["latest_day"]["rows"] == 200
    assert Decimal(report["latest_day"]["total_balance"]) == expected_total
    assert report["latest_day"]["customers"] == len(customers)
    assert sum(Decimal(b["total_balance"]) for b in report["branches"]) == expected_total


def test_report_is_reproducible(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "same")
    label = tmp_path / "label.txt"
    label.write_text("x\n")
    write_validated_mart(tmp_path / "mart")
    for out in ("a", "b"):
        build_report(["--mart-dir", str(tmp_path / "mart"), "--out", str(tmp_path / out),
                      "--release-label", str(label)])
    assert (tmp_path / "a" / "index.html").read_bytes() == (tmp_path / "b" / "index.html").read_bytes()
