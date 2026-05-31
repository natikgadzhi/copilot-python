# copilot-python

Personal CLI for exporting [Copilot Money](https://copilot.money) data to a local
SQLite database, plus a small utility for emitting CSV/Markdown summaries of
accounts and categories for annotation.

## Scripts

| Script       | Purpose                                                                  |
| ------------ | ------------------------------------------------------------------------ |
| `copilot.py` | Sync accounts, categories, and the full transactions feed into SQLite.   |
| `export.py`  | Read the SQLite DB and emit `accounts.{csv,md}` + `categories.{csv,md}`. |

Both are single-file [uv](https://docs.astral.sh/uv/) scripts with inline
[PEP 723](https://peps.python.org/pep-0723/) dependency metadata — no virtualenv
to manage.

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
uv run copilot.py                                # defaults to ./copilot.db
uv run copilot.py --db path/to/foo.db            # custom DB path
uv run copilot.py --transactions-limit 1000      # cap for fast iteration
uv run copilot.py --version
```

The transactions sync prints a running total after each page. A full sync of
tens of thousands of transactions takes a few minutes — the GraphQL feed
interleaves `Transaction` and `TransactionMonth` divider nodes, so each
200-edge page typically contains ~25 actual transactions.

Generate the annotation-friendly summaries:

```sh
uv run export.py                       # reads ./copilot.db, writes to .
uv run export.py copilot.db ./out      # custom DB + output directory
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

## Tests

```sh
uv run test_copilot.py
```

Covers CLI argument parsing and the DB helpers (idempotent upsert,
`stamp`, `ensure_local_columns`, `sweep_deleted`). No network — the
GraphQL client is not exercised.

## License

[MIT](./LICENSE)
