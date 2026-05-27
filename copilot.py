#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "sqlite-utils", "python-dotenv"]
# ///
"""Export Copilot Money data to a SQLite database.

    uv run copilot.py [DB_PATH]   # default: copilot.db

Required env (in .env or the environment):
  FIREBASE_API_KEY        public web API key (?key=AIza…) from any *.googleapis.com request
  COPILOT_REFRESH_TOKEN   from IndexedDB firebaseLocalStorageDb -> stsTokenManager.refreshToken

A fresh 1h ID token is minted at startup; no manual re-paste needed unless
the refresh token itself gets revoked.
"""
import json
import os
import sys

import httpx
import sqlite_utils
from dotenv import load_dotenv

API_URL = "https://app.copilot.money/api/graphql"
TOKEN_URL = "https://securetoken.googleapis.com/v1/token"
TRANSACTIONS_LIMIT: int | None = None


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


def sync_accounts(cp: Copilot, db: sqlite_utils.Database) -> int:
    rows = [strip_typename(a) for a in cp.gql(GET_ACCOUNTS, "Accounts")["accounts"]]
    db["accounts"].upsert_all(rows, pk="id", alter=True)
    return len(rows)


def sync_categories(cp: Copilot, db: sqlite_utils.Database) -> int:
    rows = []
    for parent in cp.gql(GET_CATEGORIES, "Categories")["categories"]:
        rows.append(flatten_category(parent, parent_id=None))
        for child in parent.get("childCategories") or []:
            rows.append(flatten_category(child, parent_id=parent["id"]))
    db["categories"].upsert_all(rows, pk="id", alter=True)
    return len(rows)


def sync_transactions(cp: Copilot, db: sqlite_utils.Database) -> int:
    after, total = None, 0
    while True:
        feed = cp.gql(GET_TRANSACTIONS, "TransactionsFeed", {"first": 200, "after": after})["feed"]
        rows = [strip_typename(e["node"]) for e in feed["edges"] if e["node"].get("__typename") == "Transaction"]
        if rows:
            db["transactions"].upsert_all(rows, pk="id", alter=True)
            total += len(rows)
        print(f"  transactions: {total}", flush=True)
        if TRANSACTIONS_LIMIT is not None and total >= TRANSACTIONS_LIMIT:
            return total
        if not feed["pageInfo"]["hasNextPage"]:
            return total
        after = feed["pageInfo"]["endCursor"]


def main() -> None:
    load_dotenv()
    db_path = sys.argv[1] if len(sys.argv) > 1 else "copilot.db"
    db = sqlite_utils.Database(db_path)
    with Copilot(os.environ["COPILOT_REFRESH_TOKEN"], os.environ["FIREBASE_API_KEY"]) as cp:
        print(f"accounts:     {sync_accounts(cp, db)}")
        print(f"categories:   {sync_categories(cp, db)}")
        print(f"transactions: {sync_transactions(cp, db)}")


if __name__ == "__main__":
    main()
