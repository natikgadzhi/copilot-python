#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest", "httpx", "sqlite-utils", "python-dotenv"]
# ///
"""Unit tests for copilot.py — CLI parsing and DB helpers, no network.

    uv run test_copilot.py
"""
import sys
from unittest.mock import patch

import pytest
import sqlite_utils

import copilot
from copilot import (
    LOCAL_COLUMNS,
    ensure_local_columns,
    flatten_category,
    parse_args,
    stamp,
    strip_typename,
    sweep_deleted,
)


# --- pure helpers -----------------------------------------------------------

def test_strip_typename_removes_typename_key():
    assert strip_typename({"id": "1", "name": "x", "__typename": "Foo"}) == {"id": "1", "name": "x"}


def test_strip_typename_passthrough_when_absent():
    assert strip_typename({"id": "1"}) == {"id": "1"}


def test_flatten_category_attaches_parent_id_and_drops_nested():
    cat = {"id": "c1", "name": "Food", "childCategories": [{"id": "c2"}], "__typename": "Category"}
    assert flatten_category(cat, parent_id="root") == {"id": "c1", "name": "Food", "parent_id": "root"}


def test_flatten_category_top_level_parent_id_is_none():
    out = flatten_category({"id": "c1", "name": "Food", "__typename": "Category"}, parent_id=None)
    assert out["parent_id"] is None


# --- stamp ------------------------------------------------------------------

def test_stamp_sets_hash_and_timestamp_on_every_row():
    rows = [{"id": "a"}, {"id": "b"}]
    stamp(rows, "2026-05-30T00:00:00Z")
    assert all(r["last_synced_at"] == "2026-05-30T00:00:00Z" for r in rows)
    assert all(len(r["remote_hash"]) == 32 for r in rows)


def test_stamp_hash_is_deterministic_for_identical_payloads():
    a, b = [{"id": "1", "name": "x"}], [{"id": "1", "name": "x"}]
    stamp(a, "ts")
    stamp(b, "ts")
    assert a[0]["remote_hash"] == b[0]["remote_hash"]


def test_stamp_hash_changes_when_payload_changes():
    a, b = [{"id": "1", "name": "x"}], [{"id": "1", "name": "y"}]
    stamp(a, "ts")
    stamp(b, "ts")
    assert a[0]["remote_hash"] != b[0]["remote_hash"]


# --- DB fixtures ------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return sqlite_utils.Database(tmp_path / "test.db")


def _seed(db, rows, table="accounts", at="2026-05-30T00:00:00Z"):
    rows = [dict(r) for r in rows]  # don't mutate caller's data
    stamp(rows, at)
    db[table].upsert_all(rows, pk="id", alter=True)
    ensure_local_columns(db, table)


# --- ensure_local_columns ---------------------------------------------------

def test_ensure_local_columns_adds_all_six(db):
    _seed(db, [{"id": "1", "name": "Acct"}])
    cols = set(db["accounts"].columns_dict)
    assert set(LOCAL_COLUMNS).issubset(cols)


def test_ensure_local_columns_is_idempotent(db):
    _seed(db, [{"id": "1", "name": "Acct"}])
    ensure_local_columns(db, "accounts")  # must not raise
    assert set(LOCAL_COLUMNS).issubset(db["accounts"].columns_dict)


def test_ensure_local_columns_skips_missing_table(db):
    ensure_local_columns(db, "doesnt_exist")  # must not raise


# --- the critical property: local edits survive resync ----------------------

def test_local_notes_and_dirty_survive_resync(db):
    _seed(db, [{"id": "1", "name": "Acct", "balance": 100}])
    db["accounts"].update("1", {"local_notes": "do not clobber", "dirty": 1})

    fresh = [{"id": "1", "name": "Acct", "balance": 200}]  # balance moved on remote
    stamp(fresh, "2026-05-30T01:00:00Z")
    db["accounts"].upsert_all(fresh, pk="id", alter=True)

    row = db["accounts"].get("1")
    assert row["balance"] == 200
    assert row["local_notes"] == "do not clobber"
    assert row["dirty"] == 1
    assert row["last_synced_at"] == "2026-05-30T01:00:00Z"  # remote cols do update


# --- sweep_deleted ----------------------------------------------------------

def test_sweep_marks_rows_not_seen_in_this_sync(db):
    _seed(db, [{"id": "1"}, {"id": "2"}])  # stamped at T0

    fresh = [{"id": "1"}]
    stamp(fresh, "2026-05-30T02:00:00Z")  # only row 1 stamped at T2
    db["accounts"].upsert_all(fresh, pk="id", alter=True)

    n = sweep_deleted(db, "accounts", "2026-05-30T02:00:00Z")
    assert n == 1
    assert db["accounts"].get("2")["deleted_at"] == "2026-05-30T02:00:00Z"
    assert db["accounts"].get("1")["deleted_at"] is None


def test_sweep_persists_to_disk(db, tmp_path):
    # Regression: an earlier version used db.execute() without commit, so the
    # rowcount looked right while the change was silently rolled back.
    _seed(db, [{"id": "1"}, {"id": "2"}])
    fresh = [{"id": "1"}]
    stamp(fresh, "2026-05-30T02:00:00Z")
    db["accounts"].upsert_all(fresh, pk="id", alter=True)
    sweep_deleted(db, "accounts", "2026-05-30T02:00:00Z")
    db.conn.close()

    reopened = sqlite_utils.Database(tmp_path / "test.db")
    assert reopened["accounts"].get("2")["deleted_at"] == "2026-05-30T02:00:00Z"


def test_sweep_ignores_already_deleted_rows(db):
    _seed(db, [{"id": "1"}])
    sweep_deleted(db, "accounts", "2026-05-30T02:00:00Z")  # marks row 1 deleted
    n = sweep_deleted(db, "accounts", "2026-05-30T03:00:00Z")  # second sweep
    assert n == 0


def test_sweep_returns_zero_when_every_row_fresh(db):
    _seed(db, [{"id": "1"}, {"id": "2"}], at="2026-05-30T02:00:00Z")
    n = sweep_deleted(db, "accounts", "2026-05-30T02:00:00Z")  # same started_at as stamps
    assert n == 0


# --- CLI --------------------------------------------------------------------

def _parse(args):
    with patch.object(sys, "argv", ["copilot.py", *args]):
        return parse_args()


def test_cli_defaults():
    args = _parse([])
    assert args.db == "copilot.db"
    assert args.transactions_limit is None


def test_cli_db_flag():
    assert _parse(["--db", "foo.db"]).db == "foo.db"


def test_cli_transactions_limit_flag():
    assert _parse(["--transactions-limit", "500"]).transactions_limit == 500


def test_cli_version_prints_version_and_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        _parse(["--version"])
    assert exc.value.code == 0
    assert copilot.__version__ in capsys.readouterr().out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
