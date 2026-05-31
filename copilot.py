#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "sqlite-utils", "python-dotenv", "typer", "rich"]
# ///
"""Copilot Money CLI: sync data into SQLite, write transaction edits back, and
export annotation-friendly summaries.

    uv run copilot.py sync [--db PATH] [--transactions-limit N]
    uv run copilot.py update TXN_ID [--name ...] [--category ...] [--description ...]
    uv run copilot.py export [--db PATH] [--out DIR]
    uv run copilot.py --version

Required env (in .env or the environment):
  FIREBASE_API_KEY        public web API key (?key=AIza…) from any *.googleapis.com request
  COPILOT_REFRESH_TOKEN   from IndexedDB firebaseLocalStorageDb -> stsTokenManager.refreshToken

A fresh 1h ID token is minted at startup; no manual re-paste needed unless
the refresh token itself gets revoked.
"""
import csv
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
import sqlite_utils
import typer
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.table import Table

__version__ = "0.4.0"

# Columns owned by this tool, never overwritten by remote upserts. local_notes
# and local_updated_at are hand-edited; the rest are bookkeeping for sync.
LOCAL_COLUMNS: dict[str, type] = {
    "local_notes": str,
    "local_updated_at": str,
    "dirty": int,
    "last_synced_at": str,
    "remote_hash": str,
    "deleted_at": str,
}

API_URL = "https://app.copilot.money/api/graphql"
TOKEN_URL = "https://securetoken.googleapis.com/v1/token"


def mint_id_token(refresh_token: str, api_key: str) -> str:
    r = httpx.post(
        TOKEN_URL,
        params={"key": api_key},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30.0,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Token refresh failed: HTTP {r.status_code}: {r.text}")
    return r.json()["id_token"]


class Copilot:
    def __init__(self, refresh_token: str, api_key: str) -> None:
        self.refresh_token = refresh_token
        self.api_key = api_key
        self.client = httpx.Client(
            base_url=API_URL,
            headers={"content-type": "application/json"},
            timeout=30.0,
        )
        self._refresh()

    def _refresh(self) -> None:
        self.client.headers["authorization"] = f"Bearer {mint_id_token(self.refresh_token, self.api_key)}"

    def gql(self, query: str, op: str, variables: dict | None = None) -> dict:
        payload = {"query": query, "variables": variables or {}, "operationName": op}
        for attempt in range(2):
            r = self.client.post("", json=payload)
            if r.status_code == 401 and attempt == 0:
                self._refresh()
                continue
            body = r.json()
            if r.status_code >= 400 or body.get("errors"):
                raise RuntimeError(f"{op} HTTP {r.status_code}: {json.dumps(body, indent=2)}")
            return body["data"]
        raise RuntimeError(f"{op}: still unauthorized after token refresh")

    def __enter__(self) -> "Copilot":
        return self

    def __exit__(self, *exc) -> None:
        self.client.close()


def strip_typename(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "__typename"}


def flatten_category(c: dict, parent_id: str | None) -> dict:
    return {k: v for k, v in c.items() if k not in ("childCategories", "__typename")} | {"parent_id": parent_id}


def ensure_local_columns(db: sqlite_utils.Database, table: str) -> None:
    if not db[table].exists():
        return
    existing = set(db[table].columns_dict)
    for col, ty in LOCAL_COLUMNS.items():
        if col not in existing:
            kwargs = {"not_null_default": 0} if col == "dirty" else {}
            db[table].add_column(col, ty, **kwargs)


def stamp(rows: list[dict], started_at: str) -> None:
    """Annotate remote rows with a deterministic hash of their payload and the
    sync timestamp. Both columns are part of the upsert so they update on
    every sync; local-only columns are absent and therefore preserved."""
    for r in rows:
        payload = json.dumps(r, sort_keys=True, default=str).encode()
        r["remote_hash"] = hashlib.md5(payload).hexdigest()
        r["last_synced_at"] = started_at


def sweep_deleted(db: sqlite_utils.Database, table: str, started_at: str) -> int:
    """Soft-delete rows we didn't see in this sync. Returns count newly marked."""
    with db.conn:
        cur = db.conn.execute(
            f"UPDATE [{table}] SET deleted_at = ? "
            "WHERE (last_synced_at IS NULL OR last_synced_at < ?) AND deleted_at IS NULL",
            [started_at, started_at],
        )
    return cur.rowcount


class CategoryError(Exception):
    """Raised when a --category name can't be resolved to exactly one live category."""


def resolve_category(db: sqlite_utils.Database, name: str) -> str:
    """Resolve a category name to its id via the local categories table.

    Raises CategoryError if no live category matches, or if the name is
    ambiguous (listing the candidate ids so the caller can disambiguate)."""
    matches = list(db["categories"].rows_where("name = ? AND deleted_at IS NULL", [name]))
    if not matches:
        raise CategoryError(f"No category named {name!r}. Run `sync` first or check the spelling.")
    if len(matches) > 1:
        candidates = ", ".join(f"{m['id']} (parent={m['parent_id']})" for m in matches)
        raise CategoryError(f"Category name {name!r} is ambiguous; matches: {candidates}.")
    return matches[0]["id"]


GET_ACCOUNTS = """
query Accounts($filter: AccountFilter, $accountLink: Boolean = false) {
  accounts(filter: $filter) {
    ...AccountFields
    accountLink @include(if: $accountLink) {
      type
      account {
        ...AccountFields
        __typename
      }
      __typename
    }
    __typename
  }
}

fragment AccountFields on Account {
  hasHistoricalUpdates
  latestBalanceUpdate
  hasLiveBalance
  institutionId
  isUserHidden
  isUserClosed
  liveBalance
  isManual
  balance
  subType
  itemId
  limit
  color
  name
  type
  mask
  id
  __typename
}
"""

GET_CATEGORIES = """
query Categories($spend: Boolean = false, $budget: Boolean = false, $rollovers: Boolean) {
  categories {
    ...CategoryFields
    spend @include(if: $spend) {
      ...SpendFields
      __typename
    }
    budget(isRolloverEnabled: $rollovers) @include(if: $budget) {
      ...BudgetFields
      __typename
    }
    childCategories {
      ...CategoryFields
      spend @include(if: $spend) {
        ...SpendFields
        __typename
      }
      budget(isRolloverEnabled: $rollovers) @include(if: $budget) {
        ...BudgetFields
        __typename
      }
      __typename
    }
    __typename
  }
}

fragment SpendMonthlyFields on CategoryMonthlySpent {
  unpaidRecurringAmount
  paidRecurringAmount
  comparisonAmount
  amount
  month
  id
  __typename
}

fragment BudgetMonthlyFields on CategoryMonthlyBudget {
  unassignedRolloverAmount
  childRolloverAmount
  unassignedAmount
  resolvedAmount
  rolloverAmount
  childAmount
  goalAmount
  amount
  month
  id
  __typename
}

fragment CategoryFields on Category {
  isRolloverDisabled
  canBeDeleted
  isExcluded
  templateId
  colorName
  icon {
    ... on EmojiUnicode {
      unicode
      __typename
    }
    ... on Genmoji {
      id
      src
      __typename
    }
    __typename
  }
  name
  id
  __typename
}

fragment SpendFields on CategorySpend {
  current {
    ...SpendMonthlyFields
    __typename
  }
  histories {
    ...SpendMonthlyFields
    __typename
  }
  __typename
}

fragment BudgetFields on CategoryBudget {
  current {
    ...BudgetMonthlyFields
    __typename
  }
  histories {
    ...BudgetMonthlyFields
    __typename
  }
  __typename
}
"""

GET_TRANSACTIONS = """
query TransactionsFeed($first: Int, $after: String, $last: Int, $before: String, $filter: TransactionFilter, $sort: [TransactionSort!], $month: Boolean = false) {
  feed: transactionsFeed(
    first: $first
    after: $after
    last: $last
    before: $before
    filter: $filter
    sort: $sort
  ) {
    edges {
      cursor
      node {
        ... on TransactionMonth @include(if: $month) {
          amount
          month
          id
          __typename
        }
        ... on Transaction {
          ...TransactionFields
          __typename
        }
        __typename
      }
      __typename
    }
    pageInfo {
      endCursor
      hasNextPage
      hasPreviousPage
      startCursor
      __typename
    }
    __typename
  }
}

fragment TagFields on Tag {
  colorName
  name
  id
  __typename
}

fragment GoalFields on Goal {
  name
  icon {
    ... on EmojiUnicode {
      unicode
      __typename
    }
    ... on Genmoji {
      id
      src
      __typename
    }
    __typename
  }
  id
  __typename
}

fragment TransactionFields on Transaction {
  suggestedCategoryIds
  hasSplitError
  recurringId
  categoryId
  isReviewed
  accountId
  createdAt
  isPending
  tipAmount
  userNotes
  parentId
  itemId
  amount
  date
  name
  type
  id
  tags {
    ...TagFields
    __typename
  }
  goal {
    ...GoalFields
    __typename
  }
  __typename
}
"""


# Editable-field names inside EditTransactionInput. `userNotes` and `categoryId`
# are confirmed by live edits; `name` is inferred (matches the Transaction field
# and input type) — re-capture a name edit if a live call ever rejects it.
DESCRIPTION_FIELD = "userNotes"

# Captured verbatim from app.copilot.money. itemId/accountId/id are top-level
# args; only the edited fields go in `input`. The response returns the full
# Transaction so the local row can be patched from it.
EDIT_TRANSACTION = """
mutation EditTransaction($itemId: ID!, $accountId: ID!, $id: ID!, $input: EditTransactionInput) {
  editTransaction(itemId: $itemId, accountId: $accountId, id: $id, input: $input) {
    transaction {
      ...TransactionFields
      __typename
    }
    __typename
  }
}

fragment TagFields on Tag {
  colorName
  name
  id
  __typename
}

fragment GoalFields on Goal {
  name
  icon {
    ... on EmojiUnicode {
      unicode
      __typename
    }
    ... on Genmoji {
      id
      src
      __typename
    }
    __typename
  }
  id
  __typename
}

fragment TransactionFields on Transaction {
  suggestedCategoryIds
  hasSplitError
  recurringId
  categoryId
  isReviewed
  accountId
  createdAt
  isPending
  tipAmount
  userNotes
  parentId
  itemId
  amount
  date
  name
  type
  id
  tags {
    ...TagFields
    __typename
  }
  goal {
    ...GoalFields
    __typename
  }
  __typename
}
"""
EDIT_TRANSACTION_OP = "EditTransaction"
EDIT_TRANSACTION_FIELD = "editTransaction"

# Sort sent with `sync --incremental` to force a newest-first feed so the
# "stop at the first already-synced transaction" early-exit is sound. Shape
# confirmed from a captured transactionsFeed request (DESC = newest first).
TRANSACTION_SORT = [{"direction": "DESC", "field": "DATE"}]


def update_transaction(
    db: sqlite_utils.Database,
    cp: Copilot,
    txn_id: str,
    *,
    name: str | None = None,
    category: str | None = None,
    description: str | None = None,
) -> dict:
    """Update a transaction's name / category / note in Copilot, then patch the
    local row from the server's response. Returns the patched local row.

    Only the provided fields are sent (partial update). Raises ValueError when
    no field is given or the transaction isn't present locally, and
    CategoryError when ``category`` can't be resolved to a single category."""
    if name is None and category is None and description is None:
        raise ValueError("Nothing to update: pass at least one of name / category / description.")

    if not db["transactions"].exists():
        raise ValueError("No transactions table in the DB — run `sync` first.")
    try:
        existing = db["transactions"].get(txn_id)
    except sqlite_utils.db.NotFoundError:
        raise ValueError(f"No transaction with id {txn_id!r} in the local DB — run `sync` first or check the id.")
    if existing.get("deleted_at") is not None:
        raise ValueError(f"Transaction {txn_id!r} is soft-deleted locally; refusing to update.")

    fields: dict = {}
    if name is not None:
        fields["name"] = name
    if category is not None:
        fields["categoryId"] = resolve_category(db, category)
    if description is not None:
        fields[DESCRIPTION_FIELD] = description

    data = cp.gql(EDIT_TRANSACTION, EDIT_TRANSACTION_OP, {
        "itemId": existing.get("itemId"),
        "accountId": existing.get("accountId"),
        "id": txn_id,
        "input": fields,
    })
    updated = strip_typename(data[EDIT_TRANSACTION_FIELD]["transaction"])
    stamp([updated], datetime.now(timezone.utc).isoformat())
    db["transactions"].upsert(updated, pk="id", alter=True)
    return db["transactions"].get(txn_id)


def sync_accounts(cp: Copilot, db: sqlite_utils.Database, started_at: str) -> int:
    rows = [strip_typename(a) for a in cp.gql(GET_ACCOUNTS, "Accounts")["accounts"]]
    stamp(rows, started_at)
    db["accounts"].upsert_all(rows, pk="id", alter=True)
    ensure_local_columns(db, "accounts")
    return len(rows)


def sync_categories(cp: Copilot, db: sqlite_utils.Database, started_at: str) -> int:
    rows = []
    for parent in cp.gql(GET_CATEGORIES, "Categories")["categories"]:
        rows.append(flatten_category(parent, parent_id=None))
        for child in parent.get("childCategories") or []:
            rows.append(flatten_category(child, parent_id=parent["id"]))
    stamp(rows, started_at)
    db["categories"].upsert_all(rows, pk="id", alter=True)
    ensure_local_columns(db, "categories")
    return len(rows)


def sync_transactions(
    cp: Copilot,
    db: sqlite_utils.Database,
    started_at: str,
    limit: int | None = None,
    incremental: bool = False,
) -> int:
    """Sync the transactions feed into SQLite, returning the number of rows
    written. With ``incremental=True`` the feed is requested newest-first and
    the sync stops at the first page that contains an already-synced
    transaction — fast catch-up on new transactions. It will NOT pick up edits
    to already-synced transactions or backdated inserts; a full sync does."""
    known: set[str] = set()
    if incremental and db["transactions"].exists():
        known = {r["id"] for r in db["transactions"].rows_where(select="id")}

    after, total, ensured = None, 0, False
    while True:
        variables = {"first": 200, "after": after}
        if incremental:
            variables["sort"] = TRANSACTION_SORT
        feed = cp.gql(GET_TRANSACTIONS, "TransactionsFeed", variables)["feed"]
        page = [strip_typename(e["node"]) for e in feed["edges"] if e["node"].get("__typename") == "Transaction"]

        rows = [r for r in page if r["id"] not in known] if incremental else page
        caught_up = incremental and len(rows) < len(page)  # saw an already-synced transaction

        if rows:
            stamp(rows, started_at)
            db["transactions"].upsert_all(rows, pk="id", alter=True)
            if not ensured:
                ensure_local_columns(db, "transactions")
                ensured = True
            total += len(rows)
        print(f"  transactions: {total}", flush=True)

        if caught_up:
            return total
        if limit is not None and total >= limit:
            return total
        if not feed["pageInfo"]["hasNextPage"]:
            return total
        after = feed["pageInfo"]["endCursor"]


# --- export (CSV / Markdown summaries) --------------------------------------

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


# --- stats ------------------------------------------------------------------

STATS_TABLES = ("accounts", "categories", "transactions")


def _format_created_at(ms: int | None) -> str | None:
    """Render a Copilot epoch-millisecond `createdAt` as a UTC ISO timestamp."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def collect_stats(db: sqlite_utils.Database) -> dict:
    """Summarize the local DB: per-table live/deleted/dirty counts, the latest
    (non-deleted) transaction, and the most recent sync time across tables."""
    tables, synced = {}, []
    for name in STATS_TABLES:
        t = db[name]
        if not t.exists():
            tables[name] = {"live": 0, "deleted": 0, "dirty": 0}
            continue
        cols = set(t.columns_dict)
        tables[name] = {
            "live": t.count_where("deleted_at is null") if "deleted_at" in cols else t.count,
            "deleted": t.count_where("deleted_at is not null") if "deleted_at" in cols else 0,
            "dirty": t.count_where("dirty = 1") if "dirty" in cols else 0,
        }
        if "last_synced_at" in cols:
            m = next(db.query(f"select max(last_synced_at) m from [{name}]"))["m"]
            if m:
                synced.append(m)

    latest = None
    if db["transactions"].exists():
        cols = set(db["transactions"].columns_dict)
        where = "where deleted_at is null" if "deleted_at" in cols else ""
        order = "order by date desc" + (", createdAt desc" if "createdAt" in cols else "")
        rows = list(db.query(f"select * from transactions {where} {order} limit 1"))
        if rows:
            r = rows[0]
            category = None
            if r.get("categoryId") and db["categories"].exists():
                cat = list(db["categories"].rows_where("id = ?", [r["categoryId"]]))
                category = cat[0]["name"] if cat else None
            latest = {
                "date": r["date"],
                "created_at": _format_created_at(r.get("createdAt")),
                "name": r.get("name"),
                "category": category,
                "description": r.get("userNotes"),
            }

    return {"tables": tables, "latest_transaction": latest, "last_synced_at": max(synced) if synced else None}


# --- CLI (Typer) ------------------------------------------------------------

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Sync Copilot Money data into SQLite, write edits back, and export summaries.",
)


def _client() -> Copilot:
    return Copilot(os.environ["COPILOT_REFRESH_TOKEN"], os.environ["FIREBASE_API_KEY"])


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"copilot {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Copilot Money CLI."""


@app.command()
def sync(
    db: str = typer.Option("copilot.db", help="Path to the SQLite database."),
    transactions_limit: int = typer.Option(
        None, "--transactions-limit", metavar="N",
        help="Stop after syncing at least N transactions (default: full sync).",
    ),
    incremental: bool = typer.Option(
        False, "--incremental",
        help="Fast catch-up: sync newest transactions first and stop at the first "
             "already-synced one. Misses edits/backdated inserts — use a full sync for those.",
    ),
) -> None:
    """Sync accounts, categories, and the transactions feed into SQLite."""
    load_dotenv()
    database = sqlite_utils.Database(db)
    started_at = datetime.now(timezone.utc).isoformat()
    with _client() as cp:
        print(f"accounts:     {sync_accounts(cp, database, started_at)}")
        print(f"categories:   {sync_categories(cp, database, started_at)}")
        print(f"transactions: {sync_transactions(cp, database, started_at, limit=transactions_limit, incremental=incremental)}")
    # Soft-delete rows the remote no longer returns. Accounts/categories always
    # sync fully, so they're always swept. The transactions sweep is skipped on
    # any partial pull (--transactions-limit or --incremental) — we deliberately
    # didn't fetch the tail, so sweeping would falsely flag it as deleted.
    print(f"soft-deleted: accounts={sweep_deleted(database, 'accounts', started_at)}, "
          f"categories={sweep_deleted(database, 'categories', started_at)}", end="")
    if transactions_limit is None and not incremental:
        print(f", transactions={sweep_deleted(database, 'transactions', started_at)}")
    else:
        print(" (transactions sweep skipped — partial sync)")


@app.command()
def update(
    txn_id: str = typer.Argument(..., help="Transaction id to update."),
    name: str = typer.Option(None, "--name", help="New transaction name."),
    category: str = typer.Option(None, "--category", help="Category name, resolved against the local DB."),
    description: str = typer.Option(None, "--description", help="Note / description text."),
    db: str = typer.Option("copilot.db", help="Path to the SQLite database."),
) -> None:
    """Update a transaction's name / category / description in Copilot."""
    if name is None and category is None and description is None:
        typer.secho(
            "Nothing to update: pass at least one of --name / --category / --description.",
            fg="red", err=True,
        )
        raise typer.Exit(1)
    load_dotenv()
    database = sqlite_utils.Database(db)
    try:
        with _client() as cp:
            row = update_transaction(
                database, cp, txn_id, name=name, category=category, description=description
            )
    except (ValueError, CategoryError) as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"updated {txn_id}: name={row.get('name')!r}, categoryId={row.get('categoryId')!r}")


@app.command()
def export(
    db: str = typer.Option("copilot.db", help="Path to the SQLite database."),
    out: str = typer.Option(".", help="Output directory for the CSV/Markdown files."),
) -> None:
    """Write accounts.{csv,md} and categories.{csv,md} from the SQLite DB."""
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        typer.echo(f"accounts:   {dump_accounts(conn, out_dir)} -> accounts.csv, accounts.md")
        typer.echo(f"categories: {dump_categories(conn, out_dir)} -> categories.csv, categories.md")
    finally:
        conn.close()


@app.command()
def stats(db: str = typer.Option("copilot.db", help="Path to the SQLite database.")) -> None:
    """Show row counts, the latest transaction, and the last sync time."""
    s = collect_stats(sqlite_utils.Database(db))
    console = Console()

    counts = Table(title="Local database", box=box.SIMPLE_HEAVY, title_justify="left")
    counts.add_column("table")
    counts.add_column("live", justify="right")
    counts.add_column("deleted", justify="right")
    counts.add_column("dirty", justify="right")
    for name in STATS_TABLES:
        c = s["tables"][name]
        counts.add_row(name, str(c["live"]), str(c["deleted"]), str(c["dirty"]))
    console.print(counts)

    lt = s["latest_transaction"]
    latest = Table(title="Latest transaction", box=box.SIMPLE_HEAVY, title_justify="left", show_header=False)
    latest.add_column("field", style="bold")
    latest.add_column("value")
    if lt:
        latest.add_row("date", lt["date"])
        latest.add_row("created", lt["created_at"] or "—")
        latest.add_row("name", lt["name"] or "—")
        latest.add_row("category", lt["category"] or "—")
        latest.add_row("description", lt["description"] or "—")
    else:
        latest.add_row("—", "no transactions yet")
    console.print(latest)

    console.print(f"[bold]last synced:[/bold] {s['last_synced_at'] or '—'}")


if __name__ == "__main__":
    app()
