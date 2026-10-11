"""Validate accounts data against the accounts YAML contract.

Phases (a phase runs only if the earlier phases passed):
  1 Structure                  accounts file readable, columns match the contract fields
  2 Values                     required, types, patterns, allowed values, uniqueness
  3 Relationships & freshness  references resolve; every snapshot_date is within the
                               freshness window ending at --as-of

--as-of is required and is never derived from the data. For the historical demo use
--as-of 2025-09-30, the last balance date of the synthetic data. Passing it shows the
extract matches that evaluation date; it does not prove current source delivery.

With --baseline, also checks the change policy against an earlier contract:
breaking changes are rejected whatever the version says (a MAJOR bump does not
approve them; see consumer_review_process), and other changes need a version bump.

Exit codes: 0 passed, 1 data or policy failed, 2 the contract file itself is invalid.

Usage:
  python scripts/validate_contract.py --data data --as-of 2025-09-30
  python scripts/validate_contract.py --data data --as-of 2025-09-30 --baseline /path/to/base/accounts.yaml
"""

import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.quality import (CheckResult, LayerResult, check_columns, check_foreign_key,  # noqa: E402
                         check_required, check_types, check_unique, format_layers,
                         is_valid_date, read_table)

FIELD_TYPES = {"string", "date"}
TOP_LEVEL_KEYS = ["name", "version", "owner", "dataset", "fields", "relationships", "freshness", "change_policy"]
SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class ContractError(Exception):
    """The contract file is missing, unreadable, or malformed."""


# --- Loading and checking the contract file itself ---

def load_contract(path):
    try:
        with open(path, encoding="utf-8") as f:
            contract = yaml.safe_load(f)
    except OSError as e:
        raise ContractError(f"cannot read {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ContractError(f"{path} is not valid YAML: {e}") from e
    problems = contract_problems(contract)
    if problems:
        raise ContractError(f"{path} is malformed:\n  " + "\n  ".join(problems))
    return contract


def contract_problems(c):
    if not isinstance(c, dict):
        return ["contract must be a YAML mapping"]
    problems = [f"missing top-level key '{k}'" for k in TOP_LEVEL_KEYS if k not in c]
    if problems:
        return problems

    if not SEMVER.fullmatch(str(c["version"])):
        problems.append(f"version '{c['version']}' is not MAJOR.MINOR.PATCH")

    fields = c["fields"]
    if not isinstance(fields, list) or not fields:
        return problems + ["fields must be a non-empty list"]
    names = []
    for i, f in enumerate(fields, 1):
        label = f"field #{i} ({f.get('name', '?')})" if isinstance(f, dict) else f"field #{i}"
        if not isinstance(f, dict):
            problems.append(f"{label} must be a mapping")
            continue
        names.append(f.get("name"))
        for key in ("name", "type", "required", "meaning"):
            if key not in f:
                problems.append(f"{label} missing '{key}'")
        if f.get("type") not in FIELD_TYPES:
            problems.append(f"{label} type must be one of {sorted(FIELD_TYPES)}")
        if not isinstance(f.get("required"), bool):
            problems.append(f"{label} required must be true or false")
        if "allowed" in f and (not isinstance(f["allowed"], list) or not f["allowed"]):
            problems.append(f"{label} allowed must be a non-empty list")
        if "pattern" in f:
            try:
                re.compile(f["pattern"])
            except (re.error, TypeError):
                problems.append(f"{label} pattern does not compile")
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        problems.append(f"duplicate field names: {sorted(duplicates)}")

    dataset = c["dataset"]
    for key in ("file", "grain", "primary_key"):
        if key not in dataset:
            problems.append(f"dataset missing '{key}'")
    for key in dataset.get("primary_key", []):
        if key not in names:
            problems.append(f"primary key '{key}' is not a field")

    for i, rel in enumerate(c["relationships"], 1):
        for side in ("child", "parent"):
            if not isinstance(rel.get(side), dict) or {"file", "field"} - rel[side].keys():
                problems.append(f"relationship #{i} {side} needs file and field")

    fresh = c["freshness"]
    if fresh.get("field") not in names:
        problems.append("freshness field is not a contract field")
    elif next(f for f in fields if f["name"] == fresh["field"]).get("type") != "date":
        problems.append("freshness field must have type date")
    if not isinstance(fresh.get("max_lag_days"), int) or fresh["max_lag_days"] < 0:
        problems.append("freshness max_lag_days must be a whole number >= 0")

    if not isinstance(c["change_policy"].get("breaking"), list):
        problems.append("change_policy must list breaking changes")
    return problems


# --- Data checks ---

def check_patterns(table, fields):
    check = CheckResult(f"{table.name}: patterns")
    for f in (f for f in fields if "pattern" in f):
        pattern = re.compile(f["pattern"])
        for row, line in zip(table.rows, table.lines):
            value = row[f["name"]]
            if value.strip() and not pattern.search(value):
                check.failures.append(f"line {line}: {f['name']} '{value}' does not match {f['pattern']}")
    return check


def check_allowed(table, fields):
    check = CheckResult(f"{table.name}: allowed values")
    for f in (f for f in fields if "allowed" in f):
        allowed = [str(v) for v in f["allowed"]]
        for row, line in zip(table.rows, table.lines):
            value = row[f["name"]]
            if value.strip() and value not in allowed:
                check.failures.append(f"line {line}: {f['name']} '{value}' not in allowed values {allowed}")
    return check


class TableCache:
    """Loads each CSV in the data folder once."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.loaded = {}

    def get(self, filename):
        if filename not in self.loaded:
            self.loaded[filename] = read_table(self.data_dir / filename, filename)
        return self.loaded[filename]


def check_relationship(rel, cache):
    child, parent = rel["child"], rel["parent"]
    name = f"{child['file']} {child['field']} -> {parent['file']} {parent['field']}"
    tables = []
    for side in (child, parent):
        table, readable = cache.get(side["file"])
        if not readable.passed:
            return CheckResult(name, readable.failures)
        if side["field"] not in table.columns:
            return CheckResult(name, [f"{side['file']} has no column '{side['field']}'"])
        tables.append(table)
    check = check_foreign_key(tables[0], child["field"], tables[1], parent["field"])
    check.name = name
    return check


def check_freshness(accounts, contract, as_of):
    """Every row's snapshot_date must satisfy as_of - max_lag_days <= snapshot_date <= as_of."""
    fresh = contract["freshness"]
    field, lag = fresh["field"], fresh["max_lag_days"]
    oldest_allowed = as_of - timedelta(days=lag)
    check = CheckResult(f"freshness: every {field} from {oldest_allowed} to {as_of} (--as-of {as_of})")
    for row, line in zip(accounts.rows, accounts.lines):
        d = date.fromisoformat(row[field])
        if d > as_of:
            check.failures.append(f"line {line}: {field} {d} is after --as-of {as_of}")
        elif d < oldest_allowed:
            check.failures.append(f"line {line}: {field} {d} is {(as_of - d).days} days before "
                                  f"--as-of {as_of} (max {lag})")
    return check


def validate_data(contract, data_dir, as_of):
    """Run the three data phases. Returns a list of LayerResult."""
    fields = contract["fields"]
    names = [f["name"] for f in fields]
    cache = TableCache(data_dir)
    accounts, readable = cache.get(contract["dataset"]["file"])

    structure = [readable]
    if readable.passed:
        structure.append(check_columns(accounts, names))
    phases = [LayerResult(1, "Structure", structure)]

    def value_checks():
        key = contract["dataset"]["primary_key"]
        checks = [
            check_required(accounts, [f["name"] for f in fields if f["required"]]),
            check_types(accounts, {f["name"]: f["type"] for f in fields if f["type"] == "date"}),
            check_patterns(accounts, fields),
            check_allowed(accounts, fields),
            check_unique(accounts, key),
        ]
        checks += [check_unique(accounts, [f["name"]]) for f in fields if f.get("unique") and [f["name"]] != key]
        return checks

    def link_checks():
        checks = [check_relationship(rel, cache) for rel in contract["relationships"]]
        checks.append(check_freshness(accounts, contract, as_of))
        return checks

    for number, name, build in ((2, "Values", value_checks), (3, "Relationships & freshness", link_checks)):
        if all(p.status == "PASS" for p in phases):
            phases.append(LayerResult(number, name, build()))
        else:
            phases.append(LayerResult(number, name, [], skipped=True))
    return phases


# --- Change policy ---

def compare_contracts(old, new):
    """Return (breaking, non_breaking) lists describing how `new` differs from `old`."""
    breaking, other = [], []
    old_fields = {f["name"]: f for f in old["fields"]}
    new_fields = {f["name"]: f for f in new["fields"]}

    for name in old_fields.keys() - new_fields.keys():
        breaking.append(f"field '{name}' removed or renamed")
    for name in sorted(new_fields.keys() - old_fields.keys()):
        if new_fields[name]["required"]:
            breaking.append(f"required field '{name}' added")
        else:
            other.append(f"optional field '{name}' added")

    for name in sorted(old_fields.keys() & new_fields.keys()):
        o, n = old_fields[name], new_fields[name]
        if o["type"] != n["type"]:
            breaking.append(f"field '{name}' type changed {o['type']} -> {n['type']}")
        if o["required"] != n["required"]:
            breaking.append(f"field '{name}' required changed {o['required']} -> {n['required']}")
        old_allowed, new_allowed = o.get("allowed"), n.get("allowed")
        if old_allowed is None or new_allowed is None:
            if old_allowed != new_allowed:
                breaking.append(f"field '{name}' allowed-values rule added or removed")
        else:
            for v in sorted(set(map(str, new_allowed)) - set(map(str, old_allowed))):
                breaking.append(f"field '{name}' allowed value '{v}' added")
            for v in sorted(set(map(str, old_allowed)) - set(map(str, new_allowed))):
                breaking.append(f"field '{name}' allowed value '{v}' removed")
        if o.get("pattern") != n.get("pattern"):
            breaking.append(f"field '{name}' pattern changed")
        if bool(o.get("unique")) != bool(n.get("unique")):
            breaking.append(f"field '{name}' unique rule changed")
        if o["meaning"] != n["meaning"]:
            other.append(f"field '{name}' meaning text changed")

    for key in ("file", "grain", "primary_key"):
        if old["dataset"].get(key) != new["dataset"].get(key):
            breaking.append(f"dataset {key} changed")
    if old["relationships"] != new["relationships"]:
        breaking.append("relationships changed")
    if old["freshness"] != new["freshness"]:
        breaking.append("freshness rule changed")
    return breaking, other


def check_change_policy(old, new):
    breaking, other = compare_contracts(old, new)
    old_v = tuple(map(int, str(old["version"]).split(".")))
    new_v = tuple(map(int, str(new["version"]).split(".")))
    check = CheckResult(f"change policy: {old['version']} -> {new['version']}")
    if new_v < old_v:
        check.failures.append(f"version went backwards ({old['version']} -> {new['version']})")
    if breaking:
        check.failures.append("breaking changes are rejected by default; a version bump does not approve them "
                              "(see change_policy.consumer_review_process):")
        check.failures.extend(f"  - {b}" for b in breaking)
    elif other and new_v <= old_v:
        check.failures.append("contract changed but version was not increased:")
        check.failures.extend(f"  - {o}" for o in other)
    return check


# --- CLI ---

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--contract", default="contracts/accounts.yaml")
    parser.add_argument("--data", default="data")
    parser.add_argument("--as-of", required=True,
                        help="evaluation date for freshness (YYYY-MM-DD); never derived from the data")
    parser.add_argument("--baseline", help="earlier contract to enforce the change policy against")
    args = parser.parse_args(argv)

    if not is_valid_date(args.as_of):
        parser.error("--as-of must be YYYY-MM-DD")
    as_of = date.fromisoformat(args.as_of)

    try:
        contract = load_contract(args.contract)
        baseline = load_contract(args.baseline) if args.baseline else None
    except ContractError as e:
        print(f"CONTRACT INVALID: {e}")
        return 2

    print(f"Contract '{contract['name']}' v{contract['version']} against {args.data}/ as of {as_of}")
    results = validate_data(contract, args.data, as_of)
    if baseline is not None:
        results.append(LayerResult(4, "Change policy", [check_change_policy(baseline, contract)]))
    print(format_layers(results))

    passed = all(r.status == "PASS" for r in results)
    print("CONTRACT PASSED" if passed else "CONTRACT FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
