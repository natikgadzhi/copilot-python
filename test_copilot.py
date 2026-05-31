#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest", "httpx", "sqlite-utils", "python-dotenv", "typer"]
# ///
"""Unit tests for copilot.py — CLI parsing and DB helpers, no network.

    uv run test_copilot.py
"""
from unittest.mock import MagicMock

import pytest
import sqlite_utils
from typer.testing import CliRunner

import copilot
from copilot import (
    LOCAL_COLUMNS,
    ensure_local_columns,
    flatten_category,
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


# --- resolve_category -------------------------------------------------------

def test_resolve_category_returns_id_for_unique_name(db):
    _seed(db, [{"id": "c1", "name": "Groceries", "parent_id": None}], table="categories")
    assert copilot.resolve_category(db, "Groceries") == "c1"


def test_resolve_category_raises_on_missing_name(db):
    _seed(db, [{"id": "c1", "name": "Groceries", "parent_id": None}], table="categories")
    with pytest.raises(copilot.CategoryError) as exc:
        copilot.resolve_category(db, "Nonexistent")
    assert "Nonexistent" in str(exc.value)


def test_resolve_category_raises_and_lists_candidates_when_ambiguous(db):
    _seed(db, [
        {"id": "c1", "name": "Coffee", "parent_id": "food"},
        {"id": "c2", "name": "Coffee", "parent_id": "fun"},
    ], table="categories")
    with pytest.raises(copilot.CategoryError) as exc:
        copilot.resolve_category(db, "Coffee")
    msg = str(exc.value)
    assert "c1" in msg and "c2" in msg  # both candidate ids surfaced for disambiguation


def test_resolve_category_ignores_soft_deleted(db):
    _seed(db, [{"id": "c1", "name": "Old", "parent_id": None}], table="categories")
    db["categories"].update("c1", {"deleted_at": "2026-05-30T00:00:00Z"})
    with pytest.raises(copilot.CategoryError):
        copilot.resolve_category(db, "Old")


# --- update_transaction -----------------------------------------------------

def test_update_transaction_requires_at_least_one_field(db):
    _seed(db, [{"id": "t1", "name": "old"}], table="transactions")
    cp = MagicMock()
    with pytest.raises(ValueError):
        copilot.update_transaction(db, cp, "t1")
    cp.gql.assert_not_called()


def test_update_transaction_fails_for_unknown_id(db):
    _seed(db, [{"id": "t1", "name": "old"}], table="transactions")
    cp = MagicMock()
    with pytest.raises(ValueError) as exc:
        copilot.update_transaction(db, cp, "nope", name="x")
    assert "nope" in str(exc.value)
    cp.gql.assert_not_called()


def test_update_transaction_fails_for_soft_deleted(db):
    _seed(db, [{"id": "t1", "name": "old"}], table="transactions")
    db["transactions"].update("t1", {"deleted_at": "2026-05-30T00:00:00Z"})
    cp = MagicMock()
    with pytest.raises(ValueError):
        copilot.update_transaction(db, cp, "t1", name="x")
    cp.gql.assert_not_called()


def test_update_transaction_resolves_category_before_calling_api(db):
    _seed(db, [{"id": "t1", "name": "old"}], table="transactions")
    cp = MagicMock()
    with pytest.raises(copilot.CategoryError):
        copilot.update_transaction(db, cp, "t1", category="Ghost")
    cp.gql.assert_not_called()


def _edit_response(txn: dict) -> dict:
    """Shape of the real editTransaction mutation response."""
    return {"editTransaction": {"transaction": {**txn, "__typename": "Transaction"}, "__typename": "EditTransactionPayload"}}


def test_update_transaction_sends_ids_top_level_and_only_provided_fields_in_input(db):
    _seed(db, [{"id": "t1", "name": "old", "accountId": "acc1", "itemId": "item1", "amount": 5}], table="transactions")
    cp = MagicMock()
    cp.gql.return_value = _edit_response({"id": "t1", "name": "New", "amount": 5})

    copilot.update_transaction(db, cp, "t1", name="New")

    variables = cp.gql.call_args.args[2]  # gql(query, op, variables)
    # itemId/accountId/id are top-level (pulled from the local row); input holds only edits
    assert variables["id"] == "t1"
    assert variables["accountId"] == "acc1"
    assert variables["itemId"] == "item1"
    assert variables["input"] == {"name": "New"}  # no categoryId / userNotes keys


def test_update_transaction_maps_category_name_to_id(db):
    _seed(db, [{"id": "c1", "name": "Groceries", "parent_id": None}], table="categories")
    _seed(db, [{"id": "t1", "name": "old", "accountId": "acc1", "itemId": "item1", "amount": 5}], table="transactions")
    cp = MagicMock()
    cp.gql.return_value = _edit_response({"id": "t1", "name": "old", "categoryId": "c1", "amount": 5})

    copilot.update_transaction(db, cp, "t1", category="Groceries")

    variables = cp.gql.call_args.args[2]
    assert variables["input"]["categoryId"] == "c1"


def test_update_transaction_maps_description_to_user_notes(db):
    _seed(db, [{"id": "t1", "name": "old", "accountId": "acc1", "itemId": "item1", "amount": 5}], table="transactions")
    cp = MagicMock()
    cp.gql.return_value = _edit_response({"id": "t1", "name": "old", "userNotes": "n", "amount": 5})

    copilot.update_transaction(db, cp, "t1", description="n")

    assert cp.gql.call_args.args[2]["input"] == {"userNotes": "n"}


def test_update_transaction_patches_local_row_from_response(db):
    _seed(db, [{"id": "c1", "name": "Groceries", "parent_id": None}], table="categories")
    _seed(db, [{"id": "t1", "name": "old", "categoryId": "x", "userNotes": None,
                "accountId": "acc1", "itemId": "item1", "amount": 5}], table="transactions")

    # server echoes back normalized values that differ from what we sent
    cp = MagicMock()
    cp.gql.return_value = _edit_response({"id": "t1", "name": "Server Name", "categoryId": "c1",
                                          "userNotes": "from server", "accountId": "acc1",
                                          "itemId": "item1", "amount": 5})

    row = copilot.update_transaction(db, cp, "t1", name="New", category="Groceries", description="note")

    # local row reflects the server response, not the values we sent
    assert row["name"] == "Server Name"
    assert row["categoryId"] == "c1"
    assert row["userNotes"] == "from server"
    assert "__typename" not in row
    assert len(row["remote_hash"]) == 32
    assert row["last_synced_at"] is not None


# --- CLI (Typer) ------------------------------------------------------------

runner = CliRunner()


def _output(result) -> str:
    """All captured text, whether or not stderr was separately captured."""
    out = result.stdout or ""
    try:
        out += result.stderr or ""
    except (ValueError, AttributeError):
        pass  # stderr mixed into stdout on this Click version
    return out


def test_cli_version_prints_version_and_exits():
    result = runner.invoke(copilot.app, ["--version"])
    assert result.exit_code == 0
    assert copilot.__version__ in _output(result)


def test_cli_help_lists_all_subcommands():
    result = runner.invoke(copilot.app, ["--help"])
    assert result.exit_code == 0
    out = _output(result)
    for cmd in ("sync", "update", "export"):
        assert cmd in out


def test_update_command_requires_a_field(tmp_path):
    # No --name/--category/--description must fail fast, before any network use.
    result = runner.invoke(copilot.app, ["update", "t1", "--db", str(tmp_path / "c.db")])
    assert result.exit_code != 0
    assert "at least one" in _output(result).lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
