import csv
import hashlib
import json
from pathlib import Path

import pytest

from scripts.generate_data import main as generate
from scripts.run_gate import MART_FILE, RESULT_FILE, main as run_gate

CONTRACT = str(Path(__file__).resolve().parent.parent / "contracts" / "accounts.yaml")


def gate(data, out, as_of="2025-09-30", contract=CONTRACT):
    return run_gate(["--data", str(data), "--as-of", as_of, "--out", str(out), "--contract", contract])


def scenario(tmp_path, name):
    data = tmp_path / name
    generate(["--out", str(data), "--scenario", name])
    return data


def result(out):
    return json.loads((out / RESULT_FILE).read_text())


def statuses(res):
    return {(s["section"], s["layer"]): s["status"] for s in res["results"]}


def test_clean_passes_and_publishes_mart(tmp_path):
    out = tmp_path / "mart"
    assert gate(scenario(tmp_path, "clean"), out) == 0
    res = result(out)
    assert res["status"] == "PASS"
    assert res["as_of"] == "2025-09-30"
    assert (res["batch_start"], res["batch_end"]) == ("2025-09-01", "2025-09-30")
    with open(out / MART_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == res["mart"]["rows"] == 6000
    assert len({(r["account_id"], r["balance_date"]) for r in rows}) == 6000
    assert res["mart"]["sha256"] == hashlib.sha256((out / MART_FILE).read_bytes()).hexdigest()


def test_clean_run_is_reproducible(tmp_path):
    data = scenario(tmp_path, "clean")
    gate(data, tmp_path / "a")
    gate(data, tmp_path / "b")
    assert (tmp_path / "a" / MART_FILE).read_bytes() == (tmp_path / "b" / MART_FILE).read_bytes()


@pytest.mark.parametrize("name, failed", [
    ("duplicate_branch", {("quality", 3)}),
    ("orphan_customer", {("contract", 3), ("quality", 3)}),
    ("missing_day", {("quality", 4)}),
    ("invalid_status", {("contract", 2)}),
    ("balance_spike", {("quality", 4)}),
])
def test_failing_scenarios_publish_no_mart(tmp_path, name, failed):
    out = tmp_path / "mart"
    assert gate(scenario(tmp_path, name), out) == 1
    res = result(out)
    assert res["status"] == "FAIL"
    assert "mart" not in res
    assert not (out / MART_FILE).exists()
    assert {k for k, v in statuses(res).items() if v == "FAIL"} == failed
    assert statuses(res)[("quality", 5)] == "SKIPPED"


def test_failed_run_removes_previous_successful_mart(tmp_path):
    out = tmp_path / "mart"
    assert gate(scenario(tmp_path, "clean"), out) == 0
    assert (out / MART_FILE).exists()
    assert gate(scenario(tmp_path, "duplicate_branch"), out) == 1
    assert not (out / MART_FILE).exists()
    assert result(out)["status"] == "FAIL"


def test_stale_data_fails_with_later_as_of(tmp_path):
    out = tmp_path / "mart"
    assert gate(scenario(tmp_path, "clean"), out, as_of="2025-10-15") == 1
    failed = {k for k, v in statuses(result(out)).items() if v == "FAIL"}
    assert failed == {("contract", 3), ("quality", 4)}
    assert not (out / MART_FILE).exists()


def test_invalid_contract_exits_2_and_removes_mart(tmp_path):
    out = tmp_path / "mart"
    data = scenario(tmp_path, "clean")
    assert gate(data, out) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [unclosed")
    assert gate(data, out, contract=str(bad)) == 2
    assert not (out / MART_FILE).exists()
    assert not (out / RESULT_FILE).exists()


def test_missing_data_folder_fails(tmp_path):
    out = tmp_path / "mart"
    assert gate(tmp_path / "nope", out) == 1
    assert not (out / MART_FILE).exists()


def test_requires_data_and_as_of(tmp_path):
    with pytest.raises(SystemExit):
        run_gate(["--as-of", "2025-09-30", "--out", str(tmp_path)])
    with pytest.raises(SystemExit):
        run_gate(["--data", str(tmp_path), "--out", str(tmp_path)])


def test_bad_as_of_still_removes_previous_mart(tmp_path):
    out = tmp_path / "mart"
    data = scenario(tmp_path, "clean")
    assert gate(data, out) == 0
    with pytest.raises(SystemExit):
        gate(data, out, as_of="2025-13-01")
    assert not (out / MART_FILE).exists()


def test_gate_never_modifies_source_files(tmp_path):
    data = scenario(tmp_path, "duplicate_branch")
    before = {p.name: p.read_bytes() for p in data.glob("*.csv")}
    gate(data, tmp_path / "mart")
    assert {p.name: p.read_bytes() for p in data.glob("*.csv")} == before
