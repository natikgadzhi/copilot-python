#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "sqlite-utils", "python-dotenv"]
# ///
"""Export Copilot Money data to a SQLite database.

    uv run copilot.py [--db PATH] [--transactions-limit N]
    uv run copilot.py --version

Required env (in .env or the environment):
  FIREBASE_API_KEY        public web API key (?key=AIza…) from any *.googleapis.com request
  COPILOT_REFRESH_TOKEN   from IndexedDB firebaseLocalStorageDb -> stsTokenManager.refreshToken

A fresh 1h ID token is minted at startup; no manual re-paste needed unless
the refresh token itself gets revoked.
"""
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone

import httpx
import sqlite_utils
from dotenv import load_dotenv

__version__ = "0.2.0"

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


def sync_transactions(cp: Copilot, db: sqlite_utils.Database, started_at: str, limit: int | None = None) -> int:
    after, total, ensured = None, 0, False
    while True:
        feed = cp.gql(GET_TRANSACTIONS, "TransactionsFeed", {"first": 200, "after": after})["feed"]
        rows = [strip_typename(e["node"]) for e in feed["edges"] if e["node"].get("__typename") == "Transaction"]
        if rows:
            stamp(rows, started_at)
            db["transactions"].upsert_all(rows, pk="id", alter=True)
            if not ensured:
                ensure_local_columns(db, "transactions")
                ensured = True
            total += len(rows)
        print(f"  transactions: {total}", flush=True)
        if limit is not None and total >= limit:
            return total
        if not feed["pageInfo"]["hasNextPage"]:
            return total
        after = feed["pageInfo"]["endCursor"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="copilot",
        description="Sync Copilot Money data into a local SQLite database.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--db", default="copilot.db", help="path to the SQLite database (default: copilot.db)")
    p.add_argument(
        "--transactions-limit",
        type=int,
        default=None,
        metavar="N",
        help="stop after syncing at least N transactions (default: full sync)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()
    db = sqlite_utils.Database(args.db)
    started_at = datetime.now(timezone.utc).isoformat()
    with Copilot(os.environ["COPILOT_REFRESH_TOKEN"], os.environ["FIREBASE_API_KEY"]) as cp:
        print(f"accounts:     {sync_accounts(cp, db, started_at)}")
        print(f"categories:   {sync_categories(cp, db, started_at)}")
        print(f"transactions: {sync_transactions(cp, db, started_at, limit=args.transactions_limit)}")
    # Soft-delete rows the remote no longer returns. Transactions are skipped
    # when --transactions-limit caps the sync — partial pulls would falsely
    # flag the un-fetched tail as deleted.
    print(f"soft-deleted: accounts={sweep_deleted(db, 'accounts', started_at)}, "
          f"categories={sweep_deleted(db, 'categories', started_at)}", end="")
    if args.transactions_limit is None:
        print(f", transactions={sweep_deleted(db, 'transactions', started_at)}")
    else:
        print(" (transactions sweep skipped — partial sync)")


if __name__ == "__main__":
    main()
