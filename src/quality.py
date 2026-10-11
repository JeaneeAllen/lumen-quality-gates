"""Layered data quality checks for Lumen Quality Gates.

Layer 1  Schema          files exist, headers match, every row has the right number of fields
Layer 2  Fields          required values present, dates and decimals parse
Layer 3  Keys            primary keys unique, foreign keys resolve
Layer 4  Completeness &  source coverage: one balance per account per day across the declared
         business rules  batch range; no day-over-day balance movement greater than 300%
Layer 5  Deposits mart   (run by scripts/run_gate.py after the contract and layers 1-4 pass)
                         join keys unique before joining; after joining, mart grain unique,
                         row count preserved, balance totals reconciled

A layer runs only if every earlier layer passed, because it relies on what the
earlier layers guarantee (for example, balances must parse before they can be
compared). Later layers are reported as SKIPPED, never as passed.

Source coverage (layer 4) and join reconciliation (layer 5) answer different questions.
Coverage asks whether the source delivered every expected balance. Reconciliation asks
whether the join lost or multiplied rows; it cannot tell whether the source was complete.

Checks report every failing line. They never drop, repair, or deduplicate records.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

SPECS = {
    "branches": {
        "file": "branches.csv",
        "grain": "one row per branch_key",
        "columns": ["branch_key", "branch_name", "region"],
        "types": {},
        "key": ["branch_key"],
    },
    "customers": {
        "file": "customers.csv",
        "grain": "one row per customer_id",
        "columns": ["customer_id", "customer_name", "segment"],
        "types": {},
        "key": ["customer_id"],
    },
    "accounts": {
        "file": "accounts.csv",
        "grain": "one row per account_id",
        "columns": ["account_id", "customer_id", "account_type", "status", "opened_date", "branch_key",
                    "snapshot_date"],
        "types": {"opened_date": "date", "snapshot_date": "date"},
        "key": ["account_id"],
    },
    "daily_balances": {
        "file": "daily_balances.csv",
        "grain": "one row per account_id and balance_date (end-of-day closing balance)",
        "columns": ["account_id", "balance_date", "balance"],
        "types": {"balance_date": "date", "balance": "decimal"},
        "key": ["account_id", "balance_date"],
    },
}

# (child table, child column, parent table, parent column)
RELATIONSHIPS = [
    ("accounts", "customer_id", "customers", "customer_id"),
    ("accounts", "branch_key", "branches", "branch_key"),
    ("daily_balances", "account_id", "accounts", "account_id"),
]

MOVEMENT_THRESHOLD = Decimal("3")  # 300%: |today - yesterday| / |yesterday|, strictly greater fails
ONE_DAY = timedelta(days=1)

# The batch is the BATCH_DAYS calendar days ending on --as-of. It is declared here and
# computed from the explicit --as-of argument, never from the dates found in the data.
# In this synthetic baseline every account in accounts.csv, active or closed, is
# expected to have exactly one closing balance on every date in the batch.
BATCH_DAYS = 30

LAYER_NAMES = {1: "Schema", 2: "Fields", 3: "Keys", 4: "Completeness & business rules", 5: "Deposits mart"}

DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
DECIMAL_PATTERN = re.compile(r"-?\d+(\.\d+)?")


@dataclass
class Table:
    name: str
    columns: list[str]
    rows: list[dict]
    lines: list[int]  # file line number of each row (header is line 1)


@dataclass
class CheckResult:
    name: str
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self):
        return not self.failures


@dataclass
class LayerResult:
    number: int
    name: str
    checks: list[CheckResult]
    skipped: bool = False

    @property
    def status(self):
        if self.skipped:
            return "SKIPPED"
        return "PASS" if all(c.passed for c in self.checks) else "FAIL"


# --- Layer 1: schema ---

def load_table(data_dir, name):
    """Read a table listed in SPECS. Returns (Table or None, CheckResult for readability)."""
    return read_table(Path(data_dir) / SPECS[name]["file"], name)


def read_table(path, name):
    """Read any CSV. Returns (Table or None, CheckResult for readability)."""
    path = Path(path)
    filename = path.name
    check = CheckResult(f"{name}: file readable")
    if not path.is_file():
        check.failures.append(f"{filename} not found in {path.parent}")
        return None, check
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = list(reader.fieldnames or [])
        rows, lines = [], []
        for row in reader:
            rows.append(row)
            lines.append(reader.line_num)
    if not columns:
        check.failures.append(f"{filename} is empty")
    elif not rows:
        check.failures.append(f"{filename} has a header but no data rows")
    return Table(name, columns, rows, lines), check


def check_columns(table, expected):
    check = CheckResult(f"{table.name}: columns")
    counts = Counter(table.columns)
    for col, n in counts.items():
        if n > 1:
            check.failures.append(f"column '{col}' appears {n} times in header")
    for col in expected:
        if col not in counts:
            check.failures.append(f"missing column '{col}'")
    for col in counts:
        if col not in expected:
            check.failures.append(f"unexpected column '{col}'")
    return check


def check_row_shape(table):
    check = CheckResult(f"{table.name}: row shape")
    expected = len(table.columns)
    for row, line in zip(table.rows, table.lines):
        if None in row:
            check.failures.append(f"line {line}: {expected + len(row[None])} fields, expected {expected}")
        elif any(v is None for v in row.values()):
            found = sum(v is not None for v in row.values())
            check.failures.append(f"line {line}: {found} fields, expected {expected}")
    return check


# --- Layer 2: fields ---

def check_required(table, columns):
    check = CheckResult(f"{table.name}: required values")
    for row, line in zip(table.rows, table.lines):
        for col in columns:
            if not row[col].strip():
                check.failures.append(f"line {line}: {col} is blank")
    return check


def is_valid_date(value):
    if not DATE_PATTERN.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_valid_decimal(value):
    return DECIMAL_PATTERN.fullmatch(value) is not None


VALIDATORS = {"date": is_valid_date, "decimal": is_valid_decimal}


def check_types(table, types):
    check = CheckResult(f"{table.name}: types")
    for row, line in zip(table.rows, table.lines):
        for col, kind in types.items():
            value = row[col]
            if value.strip() and not VALIDATORS[kind](value):  # blanks are reported by check_required
                check.failures.append(f"line {line}: {col} '{value}' is not a valid {kind}")
    return check


# --- Layer 3: keys ---

def check_unique(table, key):
    check = CheckResult(f"{table.name}: unique {'+'.join(key)}")
    seen = defaultdict(list)
    for row, line in zip(table.rows, table.lines):
        seen[tuple(row[c] for c in key)].append(line)
    for value, lines in seen.items():
        if len(lines) > 1:
            check.failures.append(f"{'/'.join(value)} appears on lines {', '.join(map(str, lines))}")
    return check


def check_foreign_key(child, column, parent, parent_column):
    check = CheckResult(f"{child.name}.{column} -> {parent.name}.{parent_column}")
    valid = {row[parent_column] for row in parent.rows}
    for row, line in zip(child.rows, child.lines):
        if row[column] not in valid:
            check.failures.append(f"line {line}: {column} '{row[column]}' not found in {parent.name}")
    return check


# --- Layer 4: business rules ---

def balance_history(balances):
    """{account_id: [(date, Decimal balance, line), ...]} sorted by date."""
    by_account = defaultdict(list)
    for row, line in zip(balances.rows, balances.lines):
        by_account[row["account_id"]].append((date.fromisoformat(row["balance_date"]), Decimal(row["balance"]), line))
    for history in by_account.values():
        history.sort()
    return by_account


def batch_range(as_of):
    """(first, last) date of the batch that ends on as_of."""
    return as_of - timedelta(days=BATCH_DAYS - 1), as_of


def date_runs(dates):
    """Format sorted dates as '2025-09-01 to 2025-09-03, 2025-09-07'."""
    runs = []
    for d in dates:
        if runs and d - runs[-1][1] == ONE_DAY:
            runs[-1][1] = d
        else:
            runs.append([d, d])
    return ", ".join(str(a) if a == b else f"{a} to {b}" for a, b in runs)


def check_balance_coverage(balances, accounts, batch_start, batch_end):
    """Source completeness: every account in accounts.csv needs a balance on every date from
    batch_start to batch_end, and no balance may fall outside that range.

    Expectations come from the declared range and the accounts list, not from the dates
    each account happens to have, so a missing first day, last day, or every day is caught.
    Duplicate account/date rows are reported by the layer 3 uniqueness check.
    """
    check = CheckResult(f"{balances.name}: one balance per account per day, {batch_start} to {batch_end}")
    expected = [batch_start + timedelta(days=i) for i in range((batch_end - batch_start).days + 1)]
    present = defaultdict(set)
    outside = defaultdict(list)
    for row, line in zip(balances.rows, balances.lines):
        d = date.fromisoformat(row["balance_date"])
        if batch_start <= d <= batch_end:
            present[row["account_id"]].add(d)
        else:
            outside[row["account_id"]].append(line)
    for account in accounts.rows:
        account_id = account["account_id"]
        if account_id not in present and account_id not in outside:
            check.failures.append(f"{account_id}: no balance rows at all")
            continue
        missing = [d for d in expected if d not in present[account_id]]
        if missing:
            check.failures.append(f"{account_id}: missing {len(missing)} day(s): {date_runs(missing)}")
        if outside[account_id]:
            lines = outside[account_id]
            check.failures.append(f"{account_id}: {len(lines)} balance row(s) outside the batch range "
                                  f"(first on line {lines[0]})")
    return check


def check_balance_movement(balances, threshold=MOVEMENT_THRESHOLD):
    """Flag any balance that moves more than `threshold` from the same account's balance one
    calendar day earlier.

    Only consecutive calendar days are compared; gaps are reported by check_balance_coverage,
    never bridged. A move away from a zero balance cannot be expressed as a percentage,
    so it is flagged.
    """
    check = CheckResult(f"{balances.name}: day-over-day balance movement <= {threshold * 100:.0f}%")
    for account_id, history in balance_history(balances).items():
        for (prev_date, prev_bal, _), (cur_date, cur_bal, line) in zip(history, history[1:]):
            if cur_date - prev_date != ONE_DAY:
                continue
            if prev_bal == 0:
                if cur_bal != 0:
                    check.failures.append(
                        f"line {line}: {account_id} {cur_date} balance {cur_bal} moved from 0.00 on {prev_date}")
                continue
            change = (cur_bal - prev_bal) / abs(prev_bal)
            if abs(change) > threshold:
                check.failures.append(
                    f"line {line}: {account_id} {cur_date} balance {cur_bal} vs {prev_bal} "
                    f"on {prev_date} ({change * 100:+.1f}%)")
    return check


# --- Runner ---

def run_layers(data_dir, batch_start, batch_end):
    """Run layers 1-4 against the CSVs in data_dir. Returns a list of LayerResult."""
    tables = {}
    schema_checks = []
    for name, spec in SPECS.items():
        table, readable = load_table(data_dir, name)
        schema_checks.append(readable)
        if readable.passed:
            tables[name] = table
            schema_checks.append(check_columns(table, spec["columns"]))
            schema_checks.append(check_row_shape(table))
    layers = [LayerResult(1, LAYER_NAMES[1], schema_checks)]

    def field_checks():
        checks = []
        for name, spec in SPECS.items():
            checks.append(check_required(tables[name], spec["columns"]))
            checks.append(check_types(tables[name], spec["types"]))
        return checks

    def key_checks():
        checks = [check_unique(tables[name], spec["key"]) for name, spec in SPECS.items()]
        for child, col, parent, parent_col in RELATIONSHIPS:
            checks.append(check_foreign_key(tables[child], col, tables[parent], parent_col))
        return checks

    def business_checks():
        return [
            check_balance_coverage(tables["daily_balances"], tables["accounts"], batch_start, batch_end),
            check_balance_movement(tables["daily_balances"]),
        ]

    for number, build in ((2, field_checks), (3, key_checks), (4, business_checks)):
        if all(layer.status == "PASS" for layer in layers):
            layers.append(LayerResult(number, LAYER_NAMES[number], build()))
        else:
            layers.append(LayerResult(number, LAYER_NAMES[number], [], skipped=True))
    return layers


def gate_passed(layers):
    return all(layer.status == "PASS" for layer in layers)


def format_layers(layers, max_failures=None):
    """Human-readable report: every check's status and its failure lines.

    max_failures limits how many lines are printed per check; the count of the rest is
    always shown, and callers keep the full list.
    """
    out = []
    for layer in layers:
        out.append(f"[{layer.status}] {layer.number} {layer.name}")
        for check in layer.checks:
            out.append(f"       {'PASS' if check.passed else 'FAIL'}  {check.name}")
            shown = check.failures if max_failures is None else check.failures[:max_failures]
            out.extend(f"             {failure}" for failure in shown)
            if len(check.failures) > len(shown):
                out.append(f"             ... and {len(check.failures) - len(shown)} more "
                           f"({len(check.failures)} total)")
    return "\n".join(out)


# --- Layer 5: deposits mart ---

MART_COLUMNS = ["account_id", "balance_date", "balance", "customer_id", "customer_name", "segment",
                "account_type", "status", "branch_key", "branch_name", "region"]
MART_KEY = ["account_id", "balance_date"]
MART_GRAIN = "one row per account_id and balance_date"

# (dimension table, join key) in join order.
JOIN_KEYS = [("accounts", "account_id"), ("customers", "customer_id"), ("branches", "branch_key")]


def load_tables(data_dir):
    """Load every table in SPECS. Only call after layer 1 has passed."""
    return {name: load_table(data_dir, name)[0] for name in SPECS}


def build_mart(tables):
    """Inner-join daily balances to accounts, customers, and branches.

    Nothing is deduplicated or filtered here: a duplicate dimension key multiplies rows and
    an unmatched key loses them. The layer 5 checks exist to detect either outcome.
    """
    index = {}
    for name, key in JOIN_KEYS:
        index[name] = defaultdict(list)
        for row in tables[name].rows:
            index[name][row[key]].append(row)
    mart = []
    for bal in tables["daily_balances"].rows:
        for acc in index["accounts"].get(bal["account_id"], []):
            for cust in index["customers"].get(acc["customer_id"], []):
                for branch in index["branches"].get(acc["branch_key"], []):
                    mart.append({
                        "account_id": bal["account_id"], "balance_date": bal["balance_date"],
                        "balance": bal["balance"],
                        "customer_id": cust["customer_id"], "customer_name": cust["customer_name"],
                        "segment": cust["segment"],
                        "account_type": acc["account_type"], "status": acc["status"],
                        "branch_key": branch["branch_key"], "branch_name": branch["branch_name"],
                        "region": branch["region"],
                    })
    return Table("deposits_mart", list(MART_COLUMNS), mart, list(range(2, len(mart) + 2)))


def check_join_keys_unique(tables):
    """Before joining: each dimension's join key must be unique, or the join would multiply rows."""
    checks = []
    for name, key in JOIN_KEYS:
        check = check_unique(tables[name], [key])
        check.name = f"before join: {check.name}"
        checks.append(check)
    return checks


def check_mart_grain(mart):
    check = check_unique(mart, MART_KEY)
    check.name = f"after join: mart grain is {MART_GRAIN}"
    return check


def check_row_count(source, mart):
    check = CheckResult(f"after join: row count preserved ({source.name} -> {mart.name})")
    if len(mart.rows) != len(source.rows):
        check.failures.append(f"{source.name} has {len(source.rows)} rows; {mart.name} has {len(mart.rows)} "
                              f"(difference {len(mart.rows) - len(source.rows):+d})")
    return check


def balance_totals(table):
    """(grand total, {balance_date: total}) using Decimal, never float."""
    total = Decimal("0")
    by_date = defaultdict(lambda: Decimal("0"))
    for row in table.rows:
        amount = Decimal(row["balance"])
        total += amount
        by_date[row["balance_date"]] += amount
    return total, by_date


def check_balance_reconciliation(source, mart):
    check = CheckResult(f"after join: balance totals reconcile ({source.name} -> {mart.name})")
    source_total, source_by_date = balance_totals(source)
    mart_total, mart_by_date = balance_totals(mart)
    if mart_total != source_total:
        check.failures.append(f"grand total: {source.name} {source_total}, {mart.name} {mart_total} "
                              f"(difference {mart_total - source_total})")
    for d in sorted(source_by_date.keys() | mart_by_date.keys()):
        if source_by_date[d] != mart_by_date[d]:
            check.failures.append(f"{d}: {source.name} {source_by_date[d]}, {mart.name} {mart_by_date[d]} "
                                  f"(difference {mart_by_date[d] - source_by_date[d]})")
    return check


def run_mart_layer(tables):
    """Layer 5. Returns (LayerResult, mart Table or None). The join runs only if its keys are unique."""
    checks = check_join_keys_unique(tables)
    if not all(c.passed for c in checks):
        return LayerResult(5, LAYER_NAMES[5], checks), None
    mart = build_mart(tables)
    source = tables["daily_balances"]
    checks += [check_mart_grain(mart), check_row_count(source, mart), check_balance_reconciliation(source, mart)]
    return LayerResult(5, LAYER_NAMES[5], checks), mart
