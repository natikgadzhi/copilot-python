# Changelog

## Unreleased

### Added
- `stats` command — per-table live / soft-deleted / dirty row counts, the latest
  transaction (date + `createdAt`), and the last sync time.
- `sync --incremental` — newest-first catch-up that stops at the first
  already-synced transaction; skips the transactions soft-delete sweep. Catches
  **new** transactions only (no `updatedAt` exists to detect edits); full `sync`
  remains the catch-all.

### Notes
- `TRANSACTION_SORT` (the newest-first sort for `--incremental`) is a placeholder
  pending a captured `transactionsFeed` sort; the logic is unit-tested regardless.

## 0.3.0 — 2026-05-30

### Added
- `update TXN_ID` command — push **name / category / description** edits for a
  single transaction back to Copilot via the `editTransaction` mutation, then
  patch the local row from the response (no re-sync needed). `--category`
  resolves **by name** against the local `categories` table; unknown or
  ambiguous names fail before anything is sent.
- Unit tests for category resolution, the `update_transaction` worker (field
  mapping, guards, response-based local patch), and Typer CLI wiring.

### Changed
- Consolidated into a **single executable** with subcommands:
  `copilot.py sync` / `update` / `export`. The CLI moved from `argparse` to
  **Typer**, so the old bare `copilot.py` is now `copilot.py sync`.
- `export.py` folded in as `copilot.py export` and removed.

### Notes
- `userNotes` (description) and `categoryId` (category) input fields are
  confirmed by live edits; `name` is inferred from the schema.
