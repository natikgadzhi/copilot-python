# copilot-auth — Mac webview login + token capture for copilot.py

Status: approved design. Author: Natik (+ Claude). Date: 2026-05-31.

## 1. Problem

`copilot.py` authenticates to Copilot Money's private GraphQL API by minting a
fresh 1h Firebase ID token from two long-lived secrets it reads from the
environment (`copilot.py:55-79,731`):

- `COPILOT_REFRESH_TOKEN` — Firebase refresh token, lives in the browser's
  IndexedDB `firebaseLocalStorageDb` under `stsTokenManager.refreshToken`.
- `FIREBASE_API_KEY` — the public web API key (`?key=AIza…`).

Today these are hand-extracted from browser DevTools and pasted into `.env`.
That is the only manual, fragile step in an otherwise automated tool. We want a
small Mac app that pops a real Copilot login webview, lets the user do the
OAuth/2FA dance, captures both secrets, and stores them where `copilot.py` can
read them — so re-auth is "open app, log in" instead of "spelunk IndexedDB".

This is the third consumer anticipated by `amazon-order-export/docs/webauth-framework-design.md`.
Per that doc we do **not** extract the shared `webauth` framework yet; we copy
~150 lines from the proven `AmazonAuth` package.

## 2. Repo & stack

New repo `~/src/natikgadzhi/copilot-auth`, mirroring `amazon-order-export`'s
scaffold:

- Swift 6, `SWIFT_STRICT_CONCURRENCY: complete`.
- XcodeGen `project.yml` + `Makefile` (+ `.swift-format`, `.githooks`, CI later).
- Builds an **`.app` bundle**, not a bare SwiftPM executable: WKWebView's
  WebContent helper process only launches from inside a real, signed bundle.
  Driven as a CLI: `Copilot-Auth.app/Contents/MacOS/copilot-auth <subcommand>`.
- Signed with Developer ID (team `9YKSA5B4FP`) so the Keychain item's no-prompt
  access survives rebuilds (macOS keys the ACL on the designated requirement for
  a real identity, on the per-build cdhash for ad-hoc). CI passes
  `CODE_SIGNING_ALLOWED=NO`.

## 3. Components

### `CopilotAuth` SwiftPM package (adapted from `AmazonAuth`)

- **`CopilotSessionSecrets`** — holds `refreshToken: String` and `apiKey: String`;
  encodes to the `webauth` `SecretBundle` JSON shape (pretty-printed, sorted
  keys). `cookies` is always `[]` for Copilot.
- **`CopilotSecretStoring`** protocol with two impls:
  - `KeychainCopilotSecretStore` — one `genericPassword` item, service
    `io.respawn.copilot`, account `copilot-session`. `write` deletes any prior
    item first.
  - `InMemoryCopilotSecretStore` — test double.
- **`CopilotAuthManager`** (`@MainActor @Observable`, `NSObject` +
  `WKNavigationDelegate`) — owns the login `WKWebView`. Same lifecycle as
  `AmazonAuthManager`, but the capture hook reads IndexedDB (see §4) instead of
  cookies. Exposes a **pure** `ingest(captured:)` taking already-extracted
  scalars so completion logic is unit-testable without a WebView.
- **`CopilotTokenProbe`** — POSTs `securetoken.googleapis.com/v1/token` with the
  two secrets to validate them (for `check`). Mirrors `copilot.py`'s
  `mint_id_token`.

### `App/` target

- `CopilotAuthApp` (`@main`, ArgumentParser root + the arg-sanitizing `main()`
  that drops `-NS…`/`-Apple…` launch args).
- `AuthenticateCommand` — runs the login window via `AppRunLoop.runLogin`.
- `CheckCommand` — plain async HTTP; reads Keychain, probes validity.
- `LoginWindow` (SwiftUI `NSViewRepresentable` host) + `AppRunLoop`.

## 4. Capture mechanism (the one new bit vs Amazon)

- `startURL` = `https://app.copilot.money/` → site redirects to login if
  unauthenticated. Persistent data store (so "remember this device" reduces
  repeat 2FA).
- After each navigation commit, run an **async** `callAsyncJavaScript` that opens
  IndexedDB `firebaseLocalStorageDb` (object store `firebaseLocalStorage`), finds
  the record whose key matches `firebase:authUser:<API_KEY>:[DEFAULT]`, and
  returns `{ apiKey: <parsed from the key>, refreshToken:
  value.stsTokenManager.refreshToken }`.
- **Completion signal**: both scalars non-empty → write Keychain → terminate.
- **Contingency** (if the IndexedDB read proves unreliable on the live site):
  intercept the `securetoken.googleapis.com` / `identitytoolkit` request — the
  `?key=` param yields the API key and the token response yields the refresh
  token. Implement IndexedDB first; it's a single read for both scalars.

> The IndexedDB recipe is the design's best read of Firebase's web-SDK storage
> layout and is **unverified against the live site**. First implementation step
> after the app launches is to confirm it in the webview's Web Inspector.

## 5. Persistence + handoff contract

Keychain item value (JSON, pretty, sorted), matching the `webauth` schema:

```json
{ "cookies": [], "values": { "refreshToken": "...", "apiKey": "AIza..." }, "capturedAt": 1717200000 }
```

- service `io.respawn.copilot`, account `copilot-session`.
- Read path (any tool): `security find-generic-password -s io.respawn.copilot -w | jq -r '.values.refreshToken'`.

**`copilot.py` change** (~15 lines + tests): add `_load_secrets()` invoked before
`_client()`. After `load_dotenv()`, if either `COPILOT_REFRESH_TOKEN` or
`FIREBASE_API_KEY` is still absent, shell out to `security find-generic-password
-s io.respawn.copilot -w`, parse the JSON, and populate the missing values from
`values.refreshToken` / `values.apiKey`. **Environment / `.env` always win** —
this preserves the CI and manual-paste paths and means the Keychain is only a
fallback source. Failures (missing `security`, no item, bad JSON) degrade to the
existing `KeyError`/clear message; they never crash differently.

Caveat: the item is app-private by default ACL, so `copilot.py`'s first
`security` read triggers a one-time Keychain prompt — user clicks **Always
Allow**, persistent thereafter. Acceptable for a personal tool and safer than
widening the ACL at write time.

## 6. `check` command behavior

- Reads the bundle; POSTs the two secrets to `securetoken.googleapis.com`.
- `200` → `[OK] session valid`, exit 0.
- `400/401` → `[FAIL] session expired — run authenticate`, exit 1.
- No Keychain item → `[FAIL] no stored session — run authenticate`, exit 2.

Mirrors `amazon-order-export check` and `slack-cli auth check`.

## 7. Testing

- **Swift** (`CopilotAuthTests`): `CopilotSessionSecrets` encode/decode
  round-trip; `InMemoryCopilotSecretStore`; pure `ingest(captured:)` fed sample
  IndexedDB JSON (present / partial / absent) — no WebView.
- **Python** (`test_copilot.py`): `_load_secrets()` with `subprocess.run`
  mocked — env-present (no shell-out), env-absent + valid bundle (populates),
  env-absent + missing item (leaves unset). Network stays mocked as today.

## 8. Out of scope (YAGNI)

- No `webauth` framework extraction (per its own recommendation — extract after a
  second consumer's diff proves the abstraction).
- No cookie capture (Copilot needs none).
- No multi-account / multi-profile.
- No notarized-DMG release pipeline initially (copy Kindle/Amazon's
  `*-release.yml` later if distribution is wanted).

## 9. Build order

1. New repo scaffold: `project.yml`, `Makefile`, `.gitignore`, `.swift-format`.
2. `CopilotAuth` package: secrets → store → probe → manager (pure ingest first,
   TDD).
3. `App/` target: root command, authenticate, check, login window, run loop.
4. `xcodegen generate` + `xcodebuild` green; Swift tests green.
5. `copilot.py` `_load_secrets()` + Python tests; ruff + pytest green.
6. Manual live verification of the IndexedDB capture (user-run; interactive 2FA).
7. READMEs in both repos.
