"""Run the Lumen quality gate and publish the deposits mart only if every check passes.

Steps:
  1. Delete any previously published mart and gate result in --out, so a failed run can
     never leave an old mart looking like the current successful output.
  2. Accounts contract at --as-of (scripts/validate_contract.py rules).
  3. Quality layers 1-4 on every source file. Balance coverage uses the batch: the
     BATCH_DAYS calendar days ending on --as-of (never dates found in the data).
  4. Layer 5 runs only if steps 2 and 3 passed: join keys unique, build the mart, then
     verify mart grain, row-count preservation, and balance-total reconciliation.
  5. Pass: write <out>/deposits_mart.csv, then <out>/gate_result.json (status PASS, mart sha256).
     Fail: write <out>/gate_result.json (status FAIL) and no mart.

Records are never deduplicated, filtered, or repaired to make a check pass.

Exit codes: 0 passed, 1 a check failed, 2 the contract file is invalid.

Usage:
  python scripts/run_gate.py --data data --as-of 2025-09-30
"""

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.validate_contract import ContractError, load_contract, validate_data  # noqa: E402
from src.quality import (BATCH_DAYS, LAYER_NAMES, MART_GRAIN, LayerResult, balance_totals,  # noqa: E402
                         batch_range, format_layers, is_valid_date, load_tables, run_layers,
                         run_mart_layer)

MART_FILE = "deposits_mart.csv"
RESULT_FILE = "gate_result.json"
MAX_PRINTED_FAILURES = 10


def clear_outputs(out):
    out.mkdir(parents=True, exist_ok=True)
    removed = []
    for name in (MART_FILE, RESULT_FILE, MART_FILE + ".tmp", RESULT_FILE + ".tmp"):
        path = out / name
        if path.exists():
            path.unlink()
            removed.append(name)
    return removed


def write_atomic(path, write):
    """Write to a temporary file, then rename, so readers never see a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        write(f)
    os.replace(tmp, path)


def write_mart(path, mart):
    def write(f):
        writer = csv.DictWriter(f, fieldnames=mart.columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(mart.rows)
    write_atomic(path, write)


def section_json(section, layers):
    return [{
        "section": section, "layer": layer.number, "name": layer.name, "status": layer.status,
        "checks": [{"name": c.name, "passed": c.passed, "failures": c.failures} for c in layer.checks],
    } for layer in layers]


def failed_names(section, layers):
    return [f"{section} {layer.number} {layer.name}" for layer in layers if layer.status == "FAIL"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", required=True, help="folder containing the source CSV files")
    parser.add_argument("--as-of", required=True, help="evaluation date (YYYY-MM-DD); also the last batch date")
    parser.add_argument("--out", default="mart", help="where the mart and gate result are written")
    parser.add_argument("--contract", default="contracts/accounts.yaml")
    args = parser.parse_args(argv)

    out = Path(args.out)
    removed = clear_outputs(out)

    if not is_valid_date(args.as_of):
        parser.error("--as-of must be YYYY-MM-DD")
    as_of = date.fromisoformat(args.as_of)
    batch_start, batch_end = batch_range(as_of)

    print(f"Lumen quality gate | data={args.data} | as-of={as_of} | "
          f"batch={batch_start} to {batch_end} ({BATCH_DAYS} days)")
    if removed:
        print(f"Removed previous outputs from {out}/: {', '.join(removed)}")

    try:
        contract = load_contract(args.contract)
    except ContractError as e:
        print(f"CONTRACT INVALID: {e}")
        print("GATE ERROR: no mart published")
        return 2

    contract_phases = validate_data(contract, args.data, as_of)
    layers = run_layers(args.data, batch_start, batch_end)
    source_ok = all(p.status == "PASS" for p in contract_phases + layers)

    mart = None
    if source_ok:
        tables = load_tables(args.data)
        mart_layer, mart = run_mart_layer(tables)
    else:
        mart_layer = LayerResult(5, LAYER_NAMES[5], [], skipped=True)
    layers.append(mart_layer)

    print(f"\n== Accounts contract '{contract['name']}' v{contract['version']} ==")
    print(format_layers(contract_phases, MAX_PRINTED_FAILURES))
    print("\n== Quality layers ==")
    print(format_layers(layers, MAX_PRINTED_FAILURES))

    passed = source_ok and mart_layer.status == "PASS"
    result = {
        "status": "PASS" if passed else "FAIL",
        "data_dir": args.data,
        "as_of": str(as_of),
        "batch_start": str(batch_start),
        "batch_end": str(batch_end),
        "contract_version": contract["version"],
        "results": section_json("contract", contract_phases) + section_json("quality", layers),
    }

    if passed:
        mart_path = out / MART_FILE
        write_mart(mart_path, mart)
        total, _ = balance_totals(mart)
        result["mart"] = {
            "file": MART_FILE,
            "grain": MART_GRAIN,
            "rows": len(mart.rows),
            "balance_total": str(total),
            "sha256": hashlib.sha256(mart_path.read_bytes()).hexdigest(),
        }

    write_atomic(out / RESULT_FILE, lambda f: f.write(json.dumps(result, indent=2) + "\n"))

    print()
    if passed:
        print(f"GATE PASSED: published {out / MART_FILE} ({len(mart.rows)} rows, "
              f"balance total {result['mart']['balance_total']})")
        return 0
    failed = failed_names("contract", contract_phases) + failed_names("quality", layers)
    print(f"GATE FAILED: {'; '.join(failed)}")
    print(f"No mart published. Full failure list: {out / RESULT_FILE}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
