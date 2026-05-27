#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Export open accounts and all categories from the SQLite DB to CSV + Markdown.

Reads the SQLite database produced by copilot.py and writes four derivative files
to OUT_DIR:

  - accounts.csv / accounts.md      open accounts only (isUserClosed = 0),
                                    grouped by account type in the markdown
  - categories.csv / categories.md  all categories, with parent/child hierarchy

The markdown files include empty "notes:" lines for hand-annotation before
feeding into downstream knowledge tools. Outputs are derivatives of the DB and
are gitignored.

    uv run export.py [DB_PATH] [OUT_DIR]   # defaults: copilot.db, .
"""
import csv
import sqlite3
import sys
from pathlib import Path

ACCOUNT_COLS = ["name", "type", "subType", "mask", "balance", "limit", "institutionId", "isManual", "id"]


def dump_accounts(conn: sqlite3.Connection, out_dir: Path) -> int:
    select = ", ".join(f'"{c}"' for c in ACCOUNT_COLS)
    rows = conn.execute(
        f'SELECT {select} FROM accounts '
        'WHERE COALESCE(isUserClosed, 0) = 0 ORDER BY type, name'
    ).fetchall()

    with (out_dir / "accounts.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(ACCOUNT_COLS)
        w.writerows(rows)

    lines = ["# Open Accounts", "", f"_{len(rows)} accounts._", ""]
    current_type = None
    for r in rows:
        d = dict(zip(ACCOUNT_COLS, r))
        if d["type"] != current_type:
            current_type = d["type"]
            lines += ["", f"## {current_type}", ""]
        mask = f" ····{d['mask']}" if d["mask"] else ""
        balance = f"{d['balance']:,.2f}" if d["balance"] is not None else "—"
        sub = f" _{d['subType']}_" if d["subType"] else ""
        lines.append(f"- **{d['name']}**{mask} —{sub} balance: `{balance}`")
        lines.append(f"  - notes: ")
    (out_dir / "accounts.md").write_text("\n".join(lines) + "\n")
    return len(rows)


def dump_categories(conn: sqlite3.Connection, out_dir: Path) -> int:
    rows = conn.execute(
        "SELECT id, name, parent_id, isExcluded FROM categories ORDER BY parent_id IS NOT NULL, name"
    ).fetchall()
    by_id = {r[0]: {"id": r[0], "name": r[1], "parent_id": r[2], "isExcluded": r[3]} for r in rows}
    children: dict[str | None, list[dict]] = {}
    for c in by_id.values():
        children.setdefault(c["parent_id"], []).append(c)
    for kids in children.values():
        kids.sort(key=lambda c: c["name"].lower())

    with (out_dir / "categories.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "parent_name", "isExcluded", "id"])
        for parent in children.get(None, []):
            w.writerow([parent["name"], "", parent["isExcluded"], parent["id"]])
            for child in children.get(parent["id"], []):
                w.writerow([child["name"], parent["name"], child["isExcluded"], child["id"]])

    lines = ["# Categories", "", f"_{len(by_id)} categories._", ""]
    for parent in children.get(None, []):
        excl = " _(excluded)_" if parent["isExcluded"] else ""
        lines.append(f"- **{parent['name']}**{excl}")
        lines.append(f"  - notes: ")
        for child in children.get(parent["id"], []):
            excl = " _(excluded)_" if child["isExcluded"] else ""
            lines.append(f"  - {child['name']}{excl}")
            lines.append(f"    - notes: ")
    (out_dir / "categories.md").write_text("\n".join(lines) + "\n")
    return len(by_id)


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "copilot.db"
    out_dir = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        print(f"accounts:   {dump_accounts(conn, out_dir)} -> accounts.csv, accounts.md")
        print(f"categories: {dump_categories(conn, out_dir)} -> categories.csv, categories.md")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
