"""Generate reproducible synthetic data for Lumen Quality Gates.

All data is fake. The same --seed always produces byte-identical CSV files.

Tables written to --out:
  branches.csv        one row per branch_key
  customers.csv       one row per customer_id
  accounts.csv        one row per account_id, linked to a customer and a branch
  daily_balances.csv  one row per account_id and balance_date: the end-of-day closing balance
                      (balances, not deposit transactions)

--as-of is the last balance date (default 2025-09-30, the documented historical evaluation
date). Balances cover the --days calendar days ending on --as-of (2025-09-01 to 2025-09-30
by default), and every accounts.csv snapshot_date equals --as-of. Accounts open before the
first balance date.

Scenarios:
  clean              Valid data that should pass every quality gate.
  schema_drift       accounts.csv column 'account_type' renamed to 'acct_type'.
  bad_type           One daily balance is not numeric ('12O.50').
  missing_required   One account has a blank customer_id.
  duplicate_account  One account row is appended a second time.
  orphan_account     One daily balance row references an account that does not exist.
  orphan_customer    One account references a customer that does not exist.
  duplicate_branch   One branch row is appended a second time (duplicate branch_key).
  invalid_status     One account has status 'frozen' (contract allows only active, closed).
  balance_spike      One account's balance jumps 400% in a single day.
  stale_snapshot     Every accounts.csv snapshot_date is 7 days before the last balance date.
  missing_day        One account's balance for one interior date is deleted (missing source data).

Each defect scenario starts from clean data and changes exactly one thing. missing_day is the
only scenario that deletes a row, because missing data is the defect it demonstrates.

Usage:
  python scripts/generate_data.py --seed 42 --out data --as-of 2025-09-30
  python scripts/generate_data.py --seed 42 --out demo_data --scenario balance_spike
"""

import argparse
import csv
import random
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

BRANCH_COLUMNS = ["branch_key", "branch_name", "region"]
CUSTOMER_COLUMNS = ["customer_id", "customer_name", "segment"]
ACCOUNT_COLUMNS = ["account_id", "customer_id", "account_type", "status", "opened_date", "branch_key",
                   "snapshot_date"]
BALANCE_COLUMNS = ["account_id", "balance_date", "balance"]

BRANCHES = [
    ("BR-01", "Lumen North", "Northern"),
    ("BR-02", "Lumen South", "Southern"),
    ("BR-03", "Lumen East", "Eastern"),
    ("BR-04", "Lumen West", "Western"),
    ("BR-05", "Lumen Central", "Central"),
]
ACCOUNT_TYPES = ["checking", "savings", "money_market"]
STATUSES = ["active", "active", "active", "active", "closed"]  # weighted toward active
SEGMENTS = ["retail", "retail", "retail", "business"]
FIRST_NAMES = ["Avery", "Blake", "Casey", "Devon", "Emery", "Finley", "Harper", "Jordan",
               "Kendall", "Logan", "Morgan", "Parker", "Quinn", "Riley", "Rowan", "Sage"]
LAST_NAMES = ["Alder", "Birch", "Cedar", "Dune", "Ember", "Fern", "Grove", "Hollow",
              "Ivy", "Juniper", "Kestrel", "Linden", "Meadow", "North", "Oak", "Pine"]

# Fixed dates keep output independent of the day the script runs.
OPEN_START = date(2020, 1, 1)
DEFAULT_AS_OF = "2025-09-30"

# Clean balances move at most 10% per day, far below the 300% movement rule.
DAILY_FACTOR_MIN = 0.90
DAILY_FACTOR_MAX = 1.10

SCENARIOS = ["clean", "schema_drift", "bad_type", "missing_required", "duplicate_account",
             "orphan_account", "orphan_customer", "duplicate_branch", "invalid_status",
             "balance_spike", "stale_snapshot", "missing_day"]
STALE_DAYS = 7


def random_date(rng, start, end):
    return start + timedelta(days=rng.randint(0, (end - start).days))


def generate_branches():
    return [dict(zip(BRANCH_COLUMNS, b)) for b in BRANCHES]


def generate_customers(rng, n_customers):
    return [{
        "customer_id": f"CUS-{i:05d}",
        "customer_name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
        "segment": rng.choice(SEGMENTS),
    } for i in range(1, n_customers + 1)]


def generate_accounts(rng, n_accounts, n_customers, snapshot_date, open_end):
    accounts = []
    for i in range(1, n_accounts + 1):
        accounts.append({
            "account_id": f"ACC-{i:05d}",
            "customer_id": f"CUS-{rng.randint(1, n_customers):05d}",
            "account_type": rng.choice(ACCOUNT_TYPES),
            "status": rng.choice(STATUSES),
            "opened_date": random_date(rng, OPEN_START, open_end).isoformat(),
            "branch_key": rng.choice(BRANCHES)[0],
            "snapshot_date": snapshot_date.isoformat(),
        })
    return accounts


def generate_balances(rng, accounts, first_date, n_days):
    balances = []
    for account in accounts:
        balance = rng.uniform(100, 25000)
        for day in range(n_days):
            if day > 0:
                balance *= rng.uniform(DAILY_FACTOR_MIN, DAILY_FACTOR_MAX)
            balances.append({
                "account_id": account["account_id"],
                "balance_date": (first_date + timedelta(days=day)).isoformat(),
                "balance": f"{balance:.2f}",
            })
    return balances


def line_of(index):
    """File line number of a data row (line 1 is the header)."""
    return index + 2


# --- Defects: each takes the tables dict, changes one thing, returns a note. ---

def defect_schema_drift(tables):
    accounts = tables["accounts"]
    accounts["columns"] = ["acct_type" if c == "account_type" else c for c in accounts["columns"]]
    for row in accounts["rows"]:
        row["acct_type"] = row.pop("account_type")
    return "accounts.csv: column 'account_type' renamed to 'acct_type'"


def defect_bad_type(tables):
    i = 11
    tables["daily_balances"]["rows"][i]["balance"] = "12O.50"
    return f"daily_balances.csv line {line_of(i)}: balance '12O.50' is not numeric (letter O)"


def defect_missing_required(tables):
    i = 4
    tables["accounts"]["rows"][i]["customer_id"] = ""
    return f"accounts.csv line {line_of(i)}: customer_id is blank"


def defect_duplicate_account(tables):
    rows = tables["accounts"]["rows"]
    rows.append(dict(rows[9]))
    return (f"accounts.csv line {line_of(len(rows) - 1)}: duplicate of account_id "
            f"{rows[9]['account_id']} (line {line_of(9)})")


def defect_orphan_account(tables):
    rows = tables["daily_balances"]["rows"]
    rows.append({"account_id": "ACC-99999", "balance_date": rows[0]["balance_date"], "balance": "500.00"})
    return f"daily_balances.csv line {line_of(len(rows) - 1)}: account_id ACC-99999 does not exist"


def defect_orphan_customer(tables):
    i = 19
    tables["accounts"]["rows"][i]["customer_id"] = "CUS-99999"
    return f"accounts.csv line {line_of(i)}: customer_id CUS-99999 does not exist in customers.csv"


def defect_duplicate_branch(tables):
    rows = tables["branches"]["rows"]
    rows.append(dict(rows[2], branch_name="Lumen East Annex"))
    return (f"branches.csv line {line_of(len(rows) - 1)}: branch_key {rows[2]['branch_key']} "
            f"duplicates line {line_of(2)}")


def defect_invalid_status(tables):
    i = 14
    tables["accounts"]["rows"][i]["status"] = "frozen"
    return f"accounts.csv line {line_of(i)}: status 'frozen' is not an allowed value"


def defect_balance_spike(tables, n_days):
    # Account 8, day 15: balance becomes 5x the previous day, a +400% movement.
    i = 7 * n_days + 14
    rows = tables["daily_balances"]["rows"]
    previous = Decimal(rows[i - 1]["balance"])
    rows[i]["balance"] = f"{previous * 5:.2f}"
    return (f"daily_balances.csv line {line_of(i)}: {rows[i]['account_id']} balance on "
            f"{rows[i]['balance_date']} is {rows[i]['balance']}, +400% from {previous}")


def defect_missing_day(tables, n_days):
    # Account 12, day 10: an interior date, so the days before and after it still exist.
    i = 11 * n_days + 9
    removed = tables["daily_balances"]["rows"].pop(i)
    return (f"daily_balances.csv: deleted {removed['account_id']} {removed['balance_date']} "
            f"(was line {line_of(i)})")


def defect_stale_snapshot(tables):
    rows = tables["accounts"]["rows"]
    last_balance_date = date.fromisoformat(rows[0]["snapshot_date"])
    stale = (last_balance_date - timedelta(days=STALE_DAYS)).isoformat()
    for row in rows:
        row["snapshot_date"] = stale
    return (f"accounts.csv: every snapshot_date set to {stale}, "
            f"{STALE_DAYS} days before the last balance date {last_balance_date}")


DEFECTS = {
    "schema_drift": defect_schema_drift,
    "bad_type": defect_bad_type,
    "missing_required": defect_missing_required,
    "duplicate_account": defect_duplicate_account,
    "orphan_account": defect_orphan_account,
    "orphan_customer": defect_orphan_customer,
    "duplicate_branch": defect_duplicate_branch,
    "invalid_status": defect_invalid_status,
    "stale_snapshot": defect_stale_snapshot,
}


def write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def generate(seed, out_dir, n_accounts, n_customers, n_days, scenario, as_of=date.fromisoformat(DEFAULT_AS_OF)):
    rng = random.Random(seed)
    first_date = as_of - timedelta(days=n_days - 1)
    accounts = generate_accounts(rng, n_accounts, n_customers, as_of, open_end=first_date - timedelta(days=1))
    balances = generate_balances(rng, accounts, first_date, n_days)
    customers = generate_customers(rng, n_customers)  # drawn last so earlier tables keep their values
    tables = {
        "branches": {"columns": list(BRANCH_COLUMNS), "rows": generate_branches()},
        "customers": {"columns": list(CUSTOMER_COLUMNS), "rows": customers},
        "accounts": {"columns": list(ACCOUNT_COLUMNS), "rows": accounts},
        "daily_balances": {"columns": list(BALANCE_COLUMNS), "rows": balances},
    }

    notes = []
    if scenario == "balance_spike":
        notes.append(defect_balance_spike(tables, n_days))
    elif scenario == "missing_day":
        notes.append(defect_missing_day(tables, n_days))
    elif scenario in DEFECTS:
        notes.append(DEFECTS[scenario](tables))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, table in tables.items():
        write_csv(out / f"{name}.csv", table["columns"], table["rows"])
    counts = {name: len(table["rows"]) for name, table in tables.items()}
    return counts, notes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="data")
    parser.add_argument("--accounts", type=int, default=200)
    parser.add_argument("--customers", type=int, default=120)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--scenario", choices=SCENARIOS, default="clean")
    parser.add_argument("--as-of", default=DEFAULT_AS_OF, help="last balance date and snapshot_date (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    if args.accounts < 20 or args.days < 20 or args.customers < 1:
        parser.error("need --accounts >= 20, --days >= 20, --customers >= 1 (defect rows need room)")
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        parser.error("--as-of must be YYYY-MM-DD")
    if as_of - timedelta(days=args.days) <= OPEN_START:
        parser.error(f"--as-of must leave room for accounts opened after {OPEN_START}")

    counts, notes = generate(args.seed, args.out, args.accounts, args.customers, args.days, args.scenario, as_of)
    print(f"scenario={args.scenario} seed={args.seed} as-of={as_of} out={args.out}")
    print("wrote " + ", ".join(f"{n} {name}" for name, n in counts.items()))
    for note in notes:
        print(f"  injected: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
