"""Build the expanded fixture dataset.

Reads (never writes) tasks/data/seed.json and tasks/data/schema.sql, merges the
handwritten rows from data/additions.json, and writes:
  data/dataset.json    JSON snapshot (input for compute_expected.py)
  data/dataset.sqlite  SQLite database with exactly the three schema tables

No randomness is used, so there is no random seed. Integrity checks live in
validate_dataset.py; this script only refuses ID collisions.

Usage: python scripts/build_dataset.py
"""
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "tasks" / "data"
DATA = ROOT / "data"

TABLES = {
    "customers": ("customer_id", ["customer_id", "segment"]),
    "orders": ("order_id", ["order_id", "customer_id", "order_date", "amount_cents"]),
    "refunds": ("refund_id", ["refund_id", "order_id", "refund_date", "amount_cents"]),
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    seed = json.loads((SOURCE / "seed.json").read_text(encoding="utf-8"))
    additions = json.loads((DATA / "additions.json").read_text(encoding="utf-8"))
    schema_sql = (SOURCE / "schema.sql").read_text(encoding="utf-8")

    dataset = {}
    seed_ids = {}
    simulated_ids = {}
    for table, (key, columns) in TABLES.items():
        seed_rows = [{c: row[c] for c in columns} for row in seed[table]]
        new_rows = [{c: row[c] for c in columns} for row in additions[table]]  # drops "note"
        seed_keys = [r[key] for r in seed_rows]
        new_keys = [r[key] for r in new_rows]
        clash = set(seed_keys) & set(new_keys)
        if clash:
            raise SystemExit(f"ID collision in {table}: {sorted(clash)}")
        dataset[table] = seed_rows + new_rows
        seed_ids[table] = seed_keys
        simulated_ids[table] = new_keys

    meta = {
        "description": "Expanded fixture dataset: original seed rows plus handwritten simulated rows.",
        "generated_by": "scripts/build_dataset.py",
        "generation_method": "Handwritten fixed list in data/additions.json merged after tasks/data/seed.json.",
        "random_seed": None,
        "randomness": "none",
        "seed_ids": seed_ids,
        "simulated_ids": simulated_ids,
        "simulated_label": additions["label"],
        "source_sha256": {p.name: sha256(p) for p in sorted(SOURCE.iterdir()) if p.is_file()},
    }
    snapshot = {"meta": meta, **dataset}
    (DATA / "dataset.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")

    db_path = DATA / "dataset.sqlite"
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(schema_sql)
        for table, (_, columns) in TABLES.items():
            placeholders = ", ".join("?" for _ in columns)
            con.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                [tuple(r[c] for c in columns) for r in dataset[table]],
            )
        con.commit()
    finally:
        con.close()

    for table in TABLES:
        print(f"{table}: {len(seed_ids[table])} seed + {len(simulated_ids[table])} simulated = {len(dataset[table])}")
    print(f"wrote {(DATA / 'dataset.json').relative_to(ROOT)}")
    print(f"wrote {db_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
