# Design: `copilot update` command

Date: 2026-05-30
Status: Approved (pending implementation plan)

## Goal

Add the ability to write transaction edits back to Copilot Money, not just sync
data down. The immediate use case: while reviewing transactions in the local
SQLite DB, update a transaction's **name**, **category**, and **description /
note**. The near-future use case: a separate app fetches Amazon orders, matches
them to transactions by amount and date, and reassigns categories in bulk — so
the update logic must be cleanly importable and reusable, not CLI-only.

## Decisions

| Question | Decision |
| --- | --- |
| CLI framework | **Typer** (added to PEP 723 deps) |
| Structure | **One executable** `copilot.py` with subcommands `sync` / `update` / `export`; `export.py` folded in and deleted |
| `--category` resolution | **By name** against the local `categories` table; fail on no match, fail on ambiguous match |
| Description target field | **To be confirmed from the captured mutation payload** (likely `userNotes`, possibly a distinct field) |
| Transaction existence check | Against the **local** `transactions` table (fast, clear errors) |
| Local DB consistency after update | **Patch the local row from the mutation's returned `Transaction`**, then re-stamp |

## Architecture / structure

`copilot.py` becomes a single Typer application:

- `sync` — current behavior verbatim (accounts, categories, transactions; soft-delete sweep; `--db`, `--transactions-limit` flags).
- `update` — new; see below.
- `export` — the current `export.py` logic (CSV/Markdown for accounts and categories), moved in as a subcommand.

Shared module-level code is unchanged in spirit: `Copilot` GraphQL client,
`mint_id_token`, `strip_typename`, `flatten_category`, `ensure_local_columns`,
`stamp`, `sweep_deleted`, and the GraphQL query constants.

- PEP 723 dependency list gains `typer`. The `#!/usr/bin/env -S uv run --script`
  shebang and inline metadata format are unchanged.
- `export.py` is deleted after its logic is absorbed.
- README's two-script table becomes a subcommand table; usage examples updated.
- Each subcommand is a thin Typer function delegating to a plain, importable
  worker function. In particular `update_transaction(...)` is a module-level
  function so the future Amazon-matching script can
  `from copilot import Copilot, update_transaction` and call it per match.

## The `update` command

```
uv run copilot.py update TXN_ID [--name TEXT] [--category NAME] [--description TEXT] [--db PATH]
```

Worker signature (illustrative):

```python
def update_transaction(
    db: sqlite_utils.Database,
    cp: Copilot,
    txn_id: str,
    *,
    name: str | None = None,
    category: str | None = None,
    description: str | None = None,
) -> dict:  # returns the patched local row
    ...
```

Flow:

1. **Require at least one field.** If `name`, `category`, and `description` are
   all `None`, error and exit non-zero.
2. **Validate the transaction exists** in the local `transactions` table. Fail
   if missing or soft-deleted (`deleted_at` set). Local check gives a fast,
   clear error; the remote mutation is the ultimate source of truth.
3. **Resolve category** (only when `--category` is given): look up by `name` in
   the local `categories` table.
   - Zero matches → fail with a "no such category" message.
   - Multiple matches → fail and print each candidate (`id`, `name`, parent) so
     the user can disambiguate.
   - Exactly one match → use its `id` as the `categoryId` to send.
4. **Build mutation variables** containing only the provided fields (partial
   update — never clobber fields the user didn't pass).
5. **Send the mutation** via `cp.gql(MUTATION, op, variables)`.
6. **Patch the local row** using the `Transaction` returned by the mutation,
   then re-`stamp` it (`remote_hash`, `last_synced_at`) so the local DB matches
   the server with no drift and no full re-sync required.

## Field mapping & the capture step (implementation step 0)

The exact mutation — operation name, GraphQL input type, and which field holds
the editable "description" — is **not yet known** and must be captured before
coding the network call:

1. In `app.copilot.money`, open DevTools → Network (filter to the `graphql`
   endpoint).
2. Edit a transaction's **name**, then its **category**, then its **note /
   description**, one at a time.
3. Copy each outgoing GraphQL request payload (`query`/`operationName`/
   `variables`).

This reveals the real mutation name, input shape, and confirms the description
field (e.g. `userNotes` vs. a distinct field). The spec carries a **placeholder
mutation** that is replaced with the captured one during implementation:

```graphql
# PLACEHOLDER — replace with captured mutation
mutation UpdateTransaction($input: <InputType>!) {
  updateTransaction(input: $input) {
    ...TransactionFields
    __typename
  }
}
```

The `TransactionFields` fragment already defined for the sync query is reused so
the response patches cleanly into the local row.

## Error handling

- Validation failures (no fields, missing transaction, unknown/ambiguous
  category) print a concise message to stderr and `raise typer.Exit(1)`.
- GraphQL/transport errors surface the existing `RuntimeError` from `cp.gql`,
  which already includes the server error body.
- 401 token refresh is already handled inside `Copilot.gql`.

## Testing (TDD)

Worker logic is tested against a temporary SQLite DB with the `Copilot` client
**mocked** (no network):

- category-by-name resolution: hit, miss, ambiguous;
- "no fields given" guard;
- missing / soft-deleted transaction guard;
- local row is patched correctly from a fake mutation response, including
  re-stamping.

The live mutation is **not** unit-tested (real API). After wiring the captured
mutation, verify manually against one real transaction and confirm the change
appears in the app and round-trips on the next `sync`.

## Out of scope (YAGNI)

- Bulk / multi-ID update in this command (single ID per invocation). The
  reusable `update_transaction` function is the seam the future Amazon-matching
  script builds bulk behavior on.
- Creating categories — `--category` only resolves existing ones; unknown names
  fail by design.
- Editing fields beyond name / category / description.
