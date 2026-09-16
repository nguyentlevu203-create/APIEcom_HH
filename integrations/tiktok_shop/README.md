# TikTok Shop Open API — Le Petit Marseillais Vietnam

Local, read-only client for the TikTok Shop Open API, built for HH's ETL /
CEO e-commerce reporting. App Secret lives only in the macOS Keychain;
tokens live only in a local, gitignored, 0600 file; every network call is
checked against a hard-coded read-only allowlist before it can be sent.

## One-time setup

```bash
cd /Users/VuIT/Desktop/APIClaude/integrations/tiktok_shop
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
python3 setup_app_secret.py     # hidden prompt, writes to macOS Keychain only
```

If you don't have `tokens.json` yet, run `python3 authorize.py` once with a
fresh authorization code from a seller-authorization redirect (see that
file's docstring). If you already have valid tokens, skip straight to:

```bash
python3 run_all.py
```

`run_all.py` is the single command that does everything: loads the App
Secret from Keychain, refreshes the access token if it's within 24h of
expiry, verifies Get Authorized Shops matches Le Petit Marseillais Vietnam
/ VN exactly, runs the 7 read-only domain tests, writes raw-data smoke-test
snapshots, runs a security audit, and writes
`HH_TIKTOK_OPEN_API_SETUP_REPORT.md`.

If the Keychain has no App Secret yet, `run_all.py` stops and prints
`READY FOR APP SECRET INPUT` instead of prompting — run
`setup_app_secret.py` yourself, then re-run `run_all.py`.

If both tokens have expired, it prints `REAUTHORIZATION REQUIRED` and stops
safely (no partial/garbage state is written).

## Security model

- **App Secret**: macOS Keychain only (service `HH_TIKTOK_SHOP_OPEN_API`,
  account = App Key). Never in source, `.env`, `tokens.json`, logs, or this
  report. `keychain.get_app_secret_or_prompt()` is the only place a
  fallback hidden prompt exists, for bootstrapping before the Keychain is
  set up.
- **Tokens**: `tokens.json`, mode `0600`, atomic writes (temp file +
  `os.replace`), gitignored. `access_token_expire_in` /
  `refresh_token_expire_in` are **absolute Unix epoch timestamps** per the
  official docs — not durations. Refresh happens proactively when less than
  24h remain (`config.REFRESH_BUFFER_SECONDS`).
- **Read-only enforcement**: `tiktok_client._enforce_allowlist()` checks
  every `(method, path)` against a fixed allowlist *before* any request is
  built. Anything else — any WRITE/PUT/PATCH/DELETE, or any path not in the
  list below — raises `SecurityError` immediately. `run_all.py` self-tests
  this guard on every run (see `self_test_allowlist_guard()`).
- **No credential logging**: app_secret, auth_code, access_token,
  refresh_token are never printed/logged. The prepared `token/get` /
  `token/refresh` request URL (which embeds app_secret/auth_code/
  refresh_token as query params) is specifically prevented from leaking
  through exception messages (`tiktok_client._auth_request`).

## Allowed endpoints (hard-coded allowlist == everything this client can call)

| Method | Path | Scope |
|---|---|---|
| GET | `/authorization/202309/shops` | `seller.authorization.info` |
| POST | `/order/202309/orders/search` | `seller.order.info` |
| GET | `/finance/202309/statements` | `seller.finance.info` |
| GET | `/analytics/202510/shop/performance/{date}/performance_per_hour` | `data.shop_analytics.public.read` |
| POST | `/product/202502/products/search` | `seller.product.basic` |
| POST | `/return_refund/202602/returns/search` | `seller.return_refund.basic` |
| POST | `/affiliate_seller/202410/orders/search` | `seller.affiliate_collaboration.read` |
| GET | `/analytics/202511/products/bestselling` | `data.bestselling.public.read` |

`test_read_apis.py` / `run_all.py` check the token's `granted_scopes`
before calling each domain — missing scope → `SKIPPED_SCOPE_NOT_GRANTED`,
never `FAIL`, and no request is sent.

**Limitation, stated plainly:** method + path + required scope for every
endpoint above match what you verified directly against the official
Partner Center docs. The exact required query/body field names for the 7
Step-8 domain tests could **not** be independently re-fetched in this
environment — `partner.tiktokshop.com/docv2` is a client-rendered app with
no public content API we could reach (confirmed by reverse-engineering its
JS bundle down to an internal `document/api_meta` call that requires an
undocumented `workspace_id` we couldn't resolve). Request bodies here are
therefore minimal, conservative connectivity-test payloads; a genuine
schema mismatch surfaces honestly as `FAIL_REQUEST_SCHEMA` rather than
being misreported as `FAIL_API` or `FAIL_AUTH`.

## Signing

`tiktok_client.sign_request()`: drop `sign`/`access_token` from params →
sort keys ascending → `path + k1v1 + k2v2 + ...` → append the **exact
body bytes actually sent** (serialized once, reused for both signing and
the HTTP request — never re-serialized) → wrap with `app_secret` on both
sides → HMAC-SHA256 keyed with `app_secret` → lowercase hex. `access_token`
travels only in the `x-tts-access-token` header, never in the signed
string. Timestamp is a 10-digit Unix second count.

## Files

| File | Purpose |
|---|---|
| `config.py` | Fixed non-secret config (App Key, expected shop/region, P0 scopes) |
| `keychain.py` | App Secret via macOS Keychain (`keyring`) |
| `setup_app_secret.py` | One-time: store App Secret in Keychain |
| `tiktok_client.py` | Signing, token exchange/refresh, allowlist-enforced signed requests |
| `token_store.py` | `tokens.json` / `shop_info.json`, atomic 0600 writes |
| `authorize.py` | One-time token exchange from a fresh authorization code |
| `test_authorized_shops.py` | Get Authorized Shops, exact shop/region match |
| `refresh_token.py` | Manual + proactive token refresh, post-refresh validation |
| `read_tests.py` | Minimal per-domain requests + PASS/FAIL/SKIPPED classification |
| `test_read_apis.py` | CLI wrapper around `read_tests.py` |
| `raw_data_smoke_test.py` | Writes `data/raw/tiktok/YYYY-MM-DD/*.json` snapshots |
| `security_audit.py` | Git-tracking check, `.gitignore` check, file-permission check |
| `report.py` | Builds `HH_TIKTOK_OPEN_API_SETUP_REPORT.md` and the terminal summary |
| `run_all.py` | **The one command**: runs everything above in order |
| `.env.example` | Non-secret config template (App Key only) |
| `.gitignore` | Keeps tokens/credentials/raw data out of Git |
