"""Read-only comparison of every original table/column against a saved backup."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def fingerprint(db, table, columns):
    query = "SELECT " + ",".join(map(identifier, columns)) + " FROM " + identifier(table)
    rows = [json.dumps(list(row), ensure_ascii=False, separators=(",", ":"),
                       default=lambda value: {"bytes_hex": value.hex()}) for row in db.execute(query)]
    payload = "\n".join(sorted(rows)).encode("utf-8")
    return {"rows": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}


def audit(backup, current):
    backup, current = Path(backup).resolve(), Path(current).resolve()
    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as before, \
            closing(sqlite3.connect(current.as_uri() + "?mode=ro", uri=True)) as after:
        tables = [row[0] for row in before.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        current_tables = {row[0] for row in after.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checks = {}
        for table in tables:
            columns = [row[1] for row in before.execute("PRAGMA table_info(" + identifier(table) + ")")]
            original = fingerprint(before, table, columns)
            try:
                present = fingerprint(after, table, columns)
            except sqlite3.OperationalError as error:
                present = {"error": str(error)}
            checks[table] = {"original_columns": columns, "before": original, "after": present,
                             "preserved": original == present}
        integrity = after.execute("PRAGMA quick_check").fetchone()[0]
        active = after.execute("SELECT count(*) FROM agent_workers WHERE state='running' OR turn_active=1").fetchone()[0]
    return {"method": "read_only_all_original_columns_sorted_row_hashes", "backup": str(backup),
            "database": str(current), "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
            "all_original_values_preserved": all(value["preserved"] for value in checks.values()),
            "original_table_count": len(tables), "additive_tables": sorted(current_tables - set(tables) - {"sqlite_sequence"}),
            "quick_check": integrity, "active_workers": active, "tables": checks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = audit(args.backup, args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "tables"}))
    raise SystemExit(0 if report["all_original_values_preserved"] and report["quick_check"] == "ok" else 1)
