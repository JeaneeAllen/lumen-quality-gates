"""Build the staging deposits report from a validated deposits mart.

The report reads only the mart and its gate result. It never re-reads source files,
rebuilds joins, or repairs data.

Before writing anything it deletes <out>/index.html and <out>/report.json, then requires:
  - <mart-dir>/deposits_mart.csv and <mart-dir>/gate_result.json exist
  - gate_result.json is valid JSON with status PASS and the expected fields
  - the mart's SHA-256 matches the hash recorded by the gate
  - the mart has the expected columns, row count, and a latest balance date equal to
    the gate's batch end
  - release_label.txt has a one-line label
If any requirement fails, no report is written.

Latest-day metrics use only the latest balance date: sum each customer's account
balances, then average those customer totals. Arithmetic is Decimal; only averages are
rounded, to cents with ROUND_HALF_UP. Totals are exact (balances are already in cents).

Exit codes: 0 report written, 1 evidence rejected.

Usage:
  python scripts/build_report.py --mart-dir mart --out site
"""

import argparse
import csv
import hashlib
import html
import json
import os
import subprocess
import sys
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.quality import MART_COLUMNS, is_valid_date  # noqa: E402

MART_FILE = "deposits_mart.csv"
RESULT_FILE = "gate_result.json"
OUTPUT_FILES = ("index.html", "report.json")
NOTICE = "Lumen staging demonstration — synthetic data."
CENT = Decimal("0.01")
ROUNDING_NOTE = ("Balances are summed exactly with decimal arithmetic. Averages are rounded "
                 "to the nearest cent, halves rounded up (ROUND_HALF_UP).")
MULTI_BRANCH_NOTE = ("Accounts are assigned to branches individually, so one customer can hold accounts "
                     "at several branches. A branch summary counts a customer at every branch where they "
                     "hold an account and uses only their balances at that branch. Branch balance totals "
                     "add up to the overall total; branch customer counts can add up to more than the "
                     "overall customer count.")


class EvidenceError(Exception):
    """The mart or gate evidence is missing, failed, malformed, or does not match."""


# --- Evidence ---

def load_gate_result(mart_dir):
    path = Path(mart_dir) / RESULT_FILE
    if not path.is_file():
        raise EvidenceError(f"{path} not found; run scripts/run_gate.py first")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise EvidenceError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(result, dict):
        raise EvidenceError(f"{path} must contain a JSON object")
    if result.get("status") != "PASS":
        raise EvidenceError(f"gate status is {result.get('status')!r}, not 'PASS'; the mart is not validated")

    problems = [f"missing '{k}'" for k in ("as_of", "batch_start", "batch_end", "contract_version", "mart")
                if k not in result]
    mart = result.get("mart")
    if isinstance(mart, dict):
        problems += [f"missing 'mart.{k}'" for k in ("file", "rows", "sha256") if k not in mart]
        if mart.get("file") != MART_FILE:
            problems.append(f"mart.file is {mart.get('file')!r}, expected {MART_FILE!r}")
    elif "mart" in result:
        problems.append("'mart' must be an object")
    for key in ("as_of", "batch_start", "batch_end"):
        if key in result and not is_valid_date(str(result[key])):
            problems.append(f"{key} {result[key]!r} is not YYYY-MM-DD")
    if problems:
        raise EvidenceError(f"{path} is malformed: " + "; ".join(problems))
    return result


def load_verified_mart(mart_dir, result):
    path = Path(mart_dir) / MART_FILE
    if not path.is_file():
        raise EvidenceError(f"{path} not found although the gate result says PASS")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != result["mart"]["sha256"]:
        raise EvidenceError(f"{path} changed after validation: SHA-256 {actual[:12]}… does not match "
                            f"recorded {str(result['mart']['sha256'])[:12]}…")
    reader = csv.DictReader(data.decode("utf-8").splitlines())
    if reader.fieldnames != MART_COLUMNS:
        raise EvidenceError(f"{path} columns {reader.fieldnames} do not match {MART_COLUMNS}")
    rows = list(reader)
    if len(rows) != result["mart"]["rows"]:
        raise EvidenceError(f"{path} has {len(rows)} rows; gate recorded {result['mart']['rows']}")
    if not rows:
        raise EvidenceError(f"{path} has no rows")
    latest = max(r["balance_date"] for r in rows)
    if latest != result["batch_end"]:
        raise EvidenceError(f"latest balance date {latest} does not match the gate's batch end {result['batch_end']}")
    return rows


def read_release_label(path):
    path = Path(path)
    if not path.is_file():
        raise EvidenceError(f"{path} not found")
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise EvidenceError(f"{path} must contain exactly one non-blank line")
    return lines[0]


def source_commit():
    """GITHUB_SHA in CI, else the local HEAD, else None. Notes uncommitted local changes."""
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return f"{head} (with uncommitted changes)" if dirty.strip() else head


# --- Metrics ---

def average(total, count):
    return (total / count).quantize(CENT, rounding=ROUND_HALF_UP) if count else Decimal("0.00")


def latest_day_metrics(rows):
    """Metrics for the latest balance date only. Customer totals are summed before averaging."""
    latest = max(r["balance_date"] for r in rows)
    day = [r for r in rows if r["balance_date"] == latest]

    customer_totals = defaultdict(Decimal)
    for r in day:
        customer_totals[r["customer_id"]] += Decimal(r["balance"])
    total = sum(customer_totals.values(), Decimal("0"))

    branches = {}
    for r in day:
        b = branches.setdefault(r["branch_key"], {
            "branch_key": r["branch_key"], "branch_name": r["branch_name"], "region": r["region"],
            "accounts": set(), "customer_totals": defaultdict(Decimal)})
        b["accounts"].add(r["account_id"])
        b["customer_totals"][r["customer_id"]] += Decimal(r["balance"])
    branch_rows = []
    for key in sorted(branches):
        b = branches[key]
        branch_total = sum(b["customer_totals"].values(), Decimal("0"))
        branch_rows.append({
            "branch_key": b["branch_key"], "branch_name": b["branch_name"], "region": b["region"],
            "accounts": len(b["accounts"]),
            "customers": len(b["customer_totals"]),
            "total_balance": str(branch_total),
            "average_customer_balance": str(average(branch_total, len(b["customer_totals"]))),
        })

    return {
        "latest_balance_date": latest,
        "counts": {
            "branches": len({r["branch_key"] for r in rows}),
            "customers": len({r["customer_id"] for r in rows}),
            "accounts": len({r["account_id"] for r in rows}),
        },
        "latest_day": {
            "rows": len(day),
            "total_balance": str(total),
            "customers": len(customer_totals),
            "average_customer_balance": str(average(total, len(customer_totals))),
        },
        "branches": branch_rows,
    }


# --- Output ---

def build_report(result, rows, label, commit):
    metrics = latest_day_metrics(rows)
    return {
        "notice": NOTICE,
        "release_label": label,
        "source_commit": commit,
        "as_of": result["as_of"],
        "batch_start": result["batch_start"],
        "batch_end": result["batch_end"],
        "contract_version": result["contract_version"],
        "mart_sha256": result["mart"]["sha256"],
        **metrics,
        "method": {
            "latest_day": "Only rows for the latest balance date are used; the batch's daily snapshots are not summed.",
            "customer_average": "Each customer's account balances are summed first; the average is taken over those customer totals.",
            "counts": "Distinct branches, customers, and accounts present in the validated mart.",
            "multi_branch_customers": MULTI_BRANCH_NOTE,
            "rounding": ROUNDING_NOTE,
        },
    }


def money(value):
    return f"${Decimal(value):,.2f}"


def render_html(report):
    e = html.escape
    commit = report["source_commit"] or "unavailable (no Git commit found)"
    latest = report["latest_day"]
    counts = report["counts"]
    branch_rows = "\n".join(
        f"""        <tr><td>{e(b['branch_key'])}</td><td>{e(b['branch_name'])}</td><td>{e(b['region'])}</td>
          <td class="num">{b['accounts']}</td><td class="num">{b['customers']}</td>
          <td class="num">{money(b['total_balance'])}</td><td class="num">{money(b['average_customer_balance'])}</td></tr>"""
        for b in report["branches"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lumen Staging Deposits</title>
<style>
  :root {{ --bg: #ffffff; --fg: #1d2330; --muted: #5b6475; --line: #d9dee7; --card: #f5f7fa; --warn: #fff4d6; --warn-fg: #6b4e00; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #12161d; --fg: #e6e9ef; --muted: #9aa3b2; --line: #2b323e; --card: #1a2029; --warn: #3a2f12; --warn-fg: #f5d58a; }}
  }}
  body {{ margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width: 960px; margin: 0 auto; padding: 24px 16px 48px; }}
  .notice {{ background: var(--warn); color: var(--warn-fg); padding: 10px 14px; border-radius: 6px; font-weight: 600; }}
  h1 {{ font-size: 1.6rem; margin: 20px 0 4px; }}
  h2 {{ font-size: 1.15rem; margin: 32px 0 8px; }}
  .meta {{ display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; color: var(--muted); }}
  .meta dd {{ margin: 0; color: var(--fg); overflow-wrap: anywhere; }}
  .tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }}
  .tile {{ background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }}
  .tile .label {{ color: var(--muted); font-size: 0.85rem; }}
  .tile .value {{ font-size: 1.35rem; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }}
  th {{ color: var(--muted); font-weight: 600; font-size: 0.85rem; }}
  .num {{ text-align: right; }}
  .notes {{ color: var(--muted); font-size: 0.9rem; }}
</style>
</head>
<body>
<main>
  <p class="notice">{e(report['notice'])}</p>
  <h1>Staging deposits report</h1>
  <dl class="meta">
    <dt>Release</dt><dd>{e(report['release_label'])}</dd>
    <dt>Evaluation date</dt><dd>{e(report['as_of'])}</dd>
    <dt>Batch range</dt><dd>{e(report['batch_start'])} to {e(report['batch_end'])}</dd>
    <dt>Latest balance date</dt><dd>{e(report['latest_balance_date'])}</dd>
    <dt>Accounts contract</dt><dd>v{e(str(report['contract_version']))}</dd>
    <dt>Source commit</dt><dd>{e(commit)}</dd>
    <dt>Mart SHA-256</dt><dd>{e(report['mart_sha256'])}</dd>
  </dl>

  <h2>Latest day: {e(report['latest_balance_date'])}</h2>
  <div class="tiles">
    <div class="tile"><div class="label">Total balance</div><div class="value">{money(latest['total_balance'])}</div></div>
    <div class="tile"><div class="label">Average customer balance</div><div class="value">{money(latest['average_customer_balance'])}</div></div>
    <div class="tile"><div class="label">Branches</div><div class="value">{counts['branches']}</div></div>
    <div class="tile"><div class="label">Customers</div><div class="value">{counts['customers']}</div></div>
    <div class="tile"><div class="label">Accounts</div><div class="value">{counts['accounts']}</div></div>
  </div>

  <h2>Branch customer balances on {e(report['latest_balance_date'])}</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Key</th><th>Branch</th><th>Region</th><th class="num">Accounts</th><th class="num">Customers</th>
        <th class="num">Total balance</th><th class="num">Avg customer balance</th></tr></thead>
      <tbody>
{branch_rows}
      </tbody>
    </table>
  </div>

  <h2>Method</h2>
  <ul class="notes">
    <li>{e(report['method']['latest_day'])}</li>
    <li>{e(report['method']['customer_average'])}</li>
    <li>{e(report['method']['counts'])}</li>
    <li>{e(report['method']['multi_branch_customers'])}</li>
    <li>{e(report['method']['rounding'])}</li>
  </ul>
</main>
</body>
</html>
"""


def write_atomic(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def clear_outputs(out):
    out.mkdir(parents=True, exist_ok=True)
    removed = []
    for name in OUTPUT_FILES + tuple(n + ".tmp" for n in OUTPUT_FILES):
        if (out / name).exists():
            (out / name).unlink()
            removed.append(name)
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mart-dir", default="mart", help="folder with deposits_mart.csv and gate_result.json")
    parser.add_argument("--out", default="site", help="folder for index.html and report.json")
    parser.add_argument("--release-label", default="release_label.txt")
    args = parser.parse_args(argv)

    out = Path(args.out)
    removed = clear_outputs(out)
    if removed:
        print(f"Removed previous report files from {out}/: {', '.join(removed)}")

    try:
        result = load_gate_result(args.mart_dir)
        rows = load_verified_mart(args.mart_dir, result)
        label = read_release_label(args.release_label)
    except EvidenceError as e:
        print(f"REPORT REJECTED: {e}")
        print("No report written.")
        return 1

    report = build_report(result, rows, label, source_commit())
    write_atomic(out / "report.json", json.dumps(report, indent=2) + "\n")
    write_atomic(out / "index.html", render_html(report))

    latest = report["latest_day"]
    print(f"Evidence verified: gate PASS, mart SHA-256 {result['mart']['sha256'][:12]}… matches")
    print(f"Release {label} | as-of {report['as_of']} | latest balance date {report['latest_balance_date']}")
    print(f"Latest-day total {latest['total_balance']} | average customer balance "
          f"{latest['average_customer_balance']} over {latest['customers']} customers")
    print(f"REPORT WRITTEN: {out / 'index.html'}, {out / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
