# copilot-python

Personal CLI for [Copilot Money](https://copilot.money): sync your data into a
local SQLite database, write transaction edits back to Copilot, and emit
CSV/Markdown summaries of accounts and categories for annotation.

## Commands

`copilot.py` is a single [uv](https://docs.astral.sh/uv/) script (inline
[PEP 723](https://peps.python.org/pep-0723/) dependency metadata — no virtualenv
to manage) with three subcommands:

| Command  | Purpose                                                                  |
| -------- | ------------------------------------------------------------------------ |
| `sync`   | Sync accounts, categories, and the full transactions feed into SQLite.   |
| `update` | Push a name / category / description change for one transaction back to Copilot. |
| `export` | Read the SQLite DB and emit `accounts.{csv,md}` + `categories.{csv,md}`.  |

## Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Create a `.env` file with two values pulled from a logged-in browser session
   on `app.copilot.money`:

   ```env
   FIREBASE_API_KEY=AIza...          # from any *.googleapis.com request (?key=)
   COPILOT_REFRESH_TOKEN=...         # IndexedDB > firebaseLocalStorageDb >
                                     # stsTokenManager.refreshToken
   ```

   The script mints a fresh 1-hour ID token at startup using the refresh token,
   so you only need to re-paste these if the refresh token itself is revoked.

## Usage

Sync everything into `copilot.db`:

```sh
uv run copilot.py sync                           # defaults to ./copilot.db
uv run copilot.py sync --db path/to/foo.db       # custom DB path
uv run copilot.py sync --transactions-limit 1000 # cap for fast iteration
uv run copilot.py --version
```

The transactions sync prints a running total after each page. A full sync of
tens of thousands of transactions takes a few minutes — the GraphQL feed
interleaves `Transaction` and `TransactionMonth` divider nodes, so each
200-edge page typically contains ~25 actual transactions.

### Updating transactions

Edit one transaction and push the change back to Copilot. Pass at least one of
`--name`, `--category`, `--description`:

```sh
uv run copilot.py update TXN_ID --category "Groceries"
uv run copilot.py update TXN_ID --name "Whole Foods" --description "weekly shop"
```

`--category` is matched **by name** against the local `categories` table (run
`sync` first), so an unknown or ambiguous name fails before anything is sent.
The transaction must exist locally, too (its `itemId` / `accountId`, which the
`editTransaction` mutation requires, are read from the local row). On success
the local row is patched from Copilot's response, so the DB stays current
without a re-sync.

The description maps to Copilot's `userNotes` field and category to `categoryId`
— both confirmed by live edits. `name` is inferred from the schema; if a live
`--name` edit is ever rejected, re-capture that edit (see below) and adjust the
`input` field name in `copilot.py`.

Generate the annotation-friendly summaries:

```sh
uv run copilot.py export                       # reads ./copilot.db, writes to .
uv run copilot.py export --db copilot.db --out ./out
```

Outputs (gitignored as DB derivatives):

- `accounts.csv` / `accounts.md` — open accounts only, grouped by type
- `categories.csv` / `categories.md` — all categories with parent/child nesting

The `.md` files include empty `notes:` lines under each item so you can annotate
them by hand before feeding into a knowledge tool.

## Schema

Tables produced by `copilot.py`:

- `accounts` — one row per linked account (open + closed + hidden)
- `categories` — flat table with a `parent_id` self-reference for the 2-level tree
- `transactions` — one row per transaction; `categoryId` / `accountId` are FKs

Schema evolution is automatic: `sqlite_utils` adds columns as the GraphQL
response grows.

### Local columns (idempotent sync)

Every synced table gets these tool-owned columns. They are never overwritten
by re-syncing — only the columns present in the GraphQL response are touched
on upsert.

| Column             | Purpose                                                              |
| ------------------ | -------------------------------------------------------------------- |
| `local_notes`      | Free-form annotations you write locally.                             |
| `local_updated_at` | When you last edited a local field.                                  |
| `dirty`            | `1` when local edits are pending push back to Copilot (future work). |
| `last_synced_at`   | Set every sync, on every row the remote returned.                    |
| `remote_hash`      | MD5 of the remote payload — for detecting remote changes.            |
| `deleted_at`       | Set when a row is no longer returned by the remote (soft delete).    |

`deleted_at` is set during the post-sync sweep. For transactions, the sweep
only runs on a full sync — `--transactions-limit` skips it to avoid falsely
marking the un-fetched tail as deleted.

## Re-capturing the API operations

The API was reverse-engineered from the web app. The `editTransaction` mutation
(`EDIT_TRANSACTION` in `copilot.py`) was captured from a live note edit; the
read queries (`GET_ACCOUNTS` / `GET_CATEGORIES` / `GET_TRANSACTIONS`) likewise.
If Copilot changes the schema and a call starts failing, re-capture:

1. Open `app.copilot.money` while logged in, DevTools → **Network**, filter to
   the `graphql` endpoint.
2. Perform the action (edit a transaction's name / category / note, or load the
   relevant view) — one change at a time.
3. Copy the JSON request payload (`operationName`, `query`, `variables`) and
   paste the `query` body into the matching constant in `copilot.py`.

For `update`, `userNotes` is the confirmed note field; if you capture a `name`
or `category` edit, confirm the `input` field names (`name`, `categoryId`) and
`DESCRIPTION_FIELD`.

## Tests

```sh
uv run test_copilot.py
```

Covers the DB helpers (idempotent upsert, `stamp`, `ensure_local_columns`,
`sweep_deleted`), category-name resolution, the `update_transaction` worker
(field mapping, guards, local patch — GraphQL client mocked), and Typer CLI
wiring (`--version`, subcommands, the `update` no-field guard). No network — the
live GraphQL calls are not exercised.

## License

[MIT](./LICENSE)
