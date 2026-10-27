"""Validate the expanded fixture dataset.

Checks data/dataset.json and data/dataset.sqlite against the rules in
tasks/data/domain.md and decisions A1-A12 in docs/assumptions.md, then runs
every case in data/deliberate_invalid.json and confirms the expected error is
detected. Errors give a non-zero exit code; warnings do not.

Usage: python scripts/validate_dataset.py
"""
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "tasks" / "data"
DATA = ROOT / "data"

SEGMENTS = {"small", "large"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TABLE_COLUMNS = {
    "customers": ["customer_id", "segment"],
    "orders": ["order_id", "customer_id", "order_date", "amount_cents"],
    "refunds": ["refund_id", "order_id", "refund_date", "amount_cents"],
}
TABLE_KEYS = {"customers": "customer_id", "orders": "order_id", "refunds": "refund_id"}


def parse_date(value):
    """Return a date for a strict YYYY-MM-DD string, else None."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def check_rows(data):
    """Row-level checks. Returns a list of (level, code, message)."""
    issues = []

    def err(code, msg):
        issues.append(("ERROR", code, msg))

    def warn(code, msg):
        issues.append(("WARN", code, msg))

    for table, key in TABLE_KEYS.items():
        seen = set()
        for row in data[table]:
            if set(row) != set(TABLE_COLUMNS[table]):
                err("bad_columns", f"{table} {row.get(key)}: columns {sorted(row)}")
            if row[key] in seen:
                err("duplicate_id", f"{table}: duplicate {row[key]}")
            seen.add(row[key])

    customers = {c["customer_id"]: c for c in data["customers"]}
    orders = {o["order_id"]: o for o in data["orders"]}

    for c in data["customers"]:
        if c["segment"] not in SEGMENTS:
            err("unknown_segment", f"customer {c['customer_id']}: segment {c['segment']!r}")

    for table, date_col in (("orders", "order_date"), ("refunds", "refund_date")):
        key = TABLE_KEYS[table]
        for row in data[table]:
            if parse_date(row[date_col]) is None:
                err("bad_date", f"{table} {row[key]}: {date_col} {row[date_col]!r}")
            amount = row["amount_cents"]
            if not isinstance(amount, int) or isinstance(amount, bool):
                err("non_integer_amount", f"{table} {row[key]}: amount_cents {amount!r}")
            elif amount < 0:
                err("negative_amount", f"{table} {row[key]}: amount_cents {amount}")
            elif amount == 0:
                warn("zero_amount", f"{table} {row[key]}: amount_cents is 0 (A4)")

    for o in data["orders"]:
        if o["customer_id"] not in customers:
            err("missing_customer", f"order {o['order_id']}: customer {o['customer_id']} not found")

    refunded = defaultdict(int)
    for r in data["refunds"]:
        order = orders.get(r["order_id"])
        if order is None:
            err("missing_order", f"refund {r['refund_id']}: order {r['order_id']} not found")
            continue
        if isinstance(r["amount_cents"], int):
            refunded[r["order_id"]] += r["amount_cents"]
        rd, od = parse_date(r["refund_date"]), parse_date(order["order_date"])
        if rd and od and rd < od:
            warn("refund_before_order", f"refund {r['refund_id']} {rd} is before order {order['order_id']} {od} (A5)")

    for order_id, total in refunded.items():
        amount = orders[order_id]["amount_cents"]
        if isinstance(amount, int) and total > amount:
            err("refund_exceeds_order", f"order {order_id}: refunds {total} > amount {amount}")

    return issues


def check_seed_and_a1(data, seed, meta):
    issues = []
    for table, key in TABLE_KEYS.items():
        by_id = {r[key]: r for r in data[table]}
        for row in seed[table]:
            if by_id.get(row[key]) != row:
                issues.append(("ERROR", "seed_changed", f"{table} {row[key]} differs from seed.json"))
        expected_ids = [r[key] for r in seed[table]]
        if meta["seed_ids"][table] != expected_ids:
            issues.append(("ERROR", "seed_ids_meta", f"meta.seed_ids[{table}] does not match seed.json"))

    seed_orders = {o["order_id"] for o in seed["orders"]}
    seed_refunds = {r["refund_id"] for r in seed["refunds"]}
    for o in data["orders"]:
        if o["order_id"] not in seed_orders and o["order_date"][:7] in ("2026-08", "2026-09"):
            issues.append(("ERROR", "a1_new_row_in_seed_month", f"order {o['order_id']} dated {o['order_date']}"))
    for r in data["refunds"]:
        if r["refund_id"] in seed_refunds:
            continue
        if r["refund_date"][:7] in ("2026-08", "2026-09"):
            issues.append(("ERROR", "a1_new_row_in_seed_month", f"refund {r['refund_id']} dated {r['refund_date']}"))
        if r["order_id"] in seed_orders:
            issues.append(("ERROR", "a12_new_refund_on_seed_order", f"refund {r['refund_id']} on seed order {r['order_id']}"))
    return issues


def check_sqlite(data, db_path):
    issues = []
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        tables = sorted(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
        if tables != sorted(TABLE_COLUMNS):
            issues.append(("ERROR", "sqlite_tables", f"tables {tables}"))
        for table, columns in TABLE_COLUMNS.items():
            db_rows = sorted(con.execute(f"SELECT {', '.join(columns)} FROM {table}"))
            json_rows = sorted(tuple(r[c] for c in columns) for r in data[table])
            if db_rows != json_rows:
                issues.append(("ERROR", "sqlite_json_mismatch", f"{table}: sqlite rows differ from dataset.json"))
        fk = list(con.execute("PRAGMA foreign_key_check"))
        if fk:
            issues.append(("ERROR", "sqlite_fk", f"foreign_key_check: {fk}"))
    finally:
        con.close()
    return issues


def check_source_hashes(meta):
    issues = []
    current = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(SOURCE.iterdir()) if p.is_file()}
    if current != meta["source_sha256"]:
        issues.append(("ERROR", "source_changed", "tasks/data/* hashes differ from those recorded at build time"))
    for name, digest in current.items():
        print(f"  sha256 {digest}  tasks/data/{name}")
    return issues


def report(title, issues):
    print(f"[{title}]")
    for level, code, msg in issues:
        print(f"  {level} {code}: {msg}")
    errors = sum(1 for i in issues if i[0] == "ERROR")
    warnings = sum(1 for i in issues if i[0] == "WARN")
    print(f"  -> {errors} error(s), {warnings} warning(s)")
    return errors


def main():
    snapshot = json.loads((DATA / "dataset.json").read_text(encoding="utf-8"))
    seed = json.loads((SOURCE / "seed.json").read_text(encoding="utf-8"))
    invalid = json.loads((DATA / "deliberate_invalid.json").read_text(encoding="utf-8"))
    data = {t: snapshot[t] for t in TABLE_COLUMNS}
    meta = snapshot["meta"]

    counts = ", ".join(f"{len(data[t])} {t}" for t in TABLE_COLUMNS)
    print(f"dataset.json: {counts}")

    errors = 0
    errors += report("row checks (dataset.json)", check_rows(data))
    errors += report("seed preserved, A1, A12", check_seed_and_a1(data, seed, meta))
    errors += report("dataset.sqlite equals dataset.json", check_sqlite(data, DATA / "dataset.sqlite"))
    print("[tasks/data unchanged since build]")
    errors += report("source hashes", check_source_hashes(meta))

    print("[deliberate_invalid cases]")
    if invalid.get("label") != "deliberate_invalid":
        print("  ERROR file is not labelled deliberate_invalid")
        errors += 1
    dataset_ids = {r[TABLE_KEYS[t]] for t in TABLE_KEYS for r in data[t]}
    for case in invalid["cases"]:
        case_data = {t: case[t] for t in TABLE_COLUMNS}
        codes = {code for level, code, _ in check_rows(case_data) if level == "ERROR"}
        detected = case["expected_error"] in codes
        leaked = {r[TABLE_KEYS[t]] for t in TABLE_KEYS for r in case_data[t]} & dataset_ids
        status = "detected" if detected else "NOT DETECTED"
        print(f"  {case['case_id']} expected {case['expected_error']}: {status} (errors raised: {sorted(codes)})")
        if not detected:
            errors += 1
        if leaked:
            print(f"  ERROR {case['case_id']} IDs found in main dataset: {sorted(leaked)}")
            errors += 1

    print(f"RESULT: {'FAIL' if errors else 'PASS'} ({errors} error(s))")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
