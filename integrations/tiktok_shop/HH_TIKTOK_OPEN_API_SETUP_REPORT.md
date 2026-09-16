# HH TikTok Shop Open API — Setup Report

Generated: 2026-09-08T07:05:09.367428+00:00
App Key: 6jh02dvvivnis

## EXECUTIVE STATUS

- Token Exchange: PASS
- Token Expiry Logic: PASS (epoch-timestamp semantics, no duration-add bug)
- Token Auto Refresh: PASS_NOT_NEEDED
- Secret in macOS Keychain: PASS
- Granted P0 Scopes: 8/8
- Read-only Code Guard: PASS

- Authorized Shops: PASS
- Expected Seller: Le Petit Marseillais Vietnam
- Region: VN
- Shop Cipher: FOUND

- Orders API: PASS
- Finance API: PASS
- Analytics API: PASS_EMPTY
- Product API: PASS
- Return API: PASS
- Affiliate API: PASS
- Bestsellers API: PASS

- Raw Smoke Test: PASS
- Credential Leak Check: PASS

## Overall verdict: PRODUCTION API READY

## Details

### P0 Scopes

| SCOPE | GRANTED |
|---|---|
| seller.authorization.info | YES |
| seller.order.info | YES |
| seller.finance.info | YES |
| data.shop_analytics.public.read | YES |
| seller.product.basic | YES |
| seller.return_refund.basic | YES |
| seller.affiliate_collaboration.read | YES |
| data.bestselling.public.read | YES |

### Read API domain tests

| DOMAIN | METHOD | PATH | SCOPE | HTTP | CODE | ROWS | STATUS |
|---|---|---|---|---|---|---|---|
| orders | POST | `/order/202309/orders/search` | seller.order.info | 200 | 0 | 1 | **PASS** |
| finance | GET | `/finance/202309/statements` | seller.finance.info | 200 | 0 | 1 | **PASS** |
| analytics | GET | `/analytics/202510/shop/performance/{date}/performance_per_hour` | data.shop_analytics.public.read | 200 | 0 | 0 | **PASS_EMPTY** |
| product | POST | `/product/202502/products/search` | seller.product.basic | 200 | 0 | 1 | **PASS** |
| return_refund | POST | `/return_refund/202602/returns/search` | seller.return_refund.basic | 200 | 0 | 10 | **PASS** |
| affiliate | POST | `/affiliate_seller/202410/orders/search` | seller.affiliate_collaboration.read | 200 | 0 | 1 | **PASS** |
| bestsellers | GET | `/analytics/202511/products/bestselling` | data.bestselling.public.read | 200 | 0 | 100 | **PASS** |

Note: request bodies are minimal connectivity-test payloads; exact official field-level schemas could not be independently re-verified against live docs in this environment (the docs site is a client-rendered SPA with no reachable public content API). FAIL_REQUEST_SCHEMA specifically flags this class of risk.

### Security audit

- Git repository detected: False
- Secret file ever tracked in Git: False
- .gitignore covers required patterns: True
- tokens.json/shop_info.json permissions 0600: True
  - tokens.json: 0o600
  - shop_info.json: 0o600
- No .git directory found in this project or any parent — not a git repository, so no secret file could ever have been tracked.

### Raw smoke-test files written

- orders: `/Users/VuIT/Desktop/APIClaude/integrations/tiktok_shop/data/raw/tiktok/2026-09-08/orders.json`
- finance: `/Users/VuIT/Desktop/APIClaude/integrations/tiktok_shop/data/raw/tiktok/2026-09-08/finance_statements.json`
- analytics: `/Users/VuIT/Desktop/APIClaude/integrations/tiktok_shop/data/raw/tiktok/2026-09-08/shop_analytics.json`
- product: `/Users/VuIT/Desktop/APIClaude/integrations/tiktok_shop/data/raw/tiktok/2026-09-08/products.json`
- return_refund: `/Users/VuIT/Desktop/APIClaude/integrations/tiktok_shop/data/raw/tiktok/2026-09-08/returns.json`
- affiliate: `/Users/VuIT/Desktop/APIClaude/integrations/tiktok_shop/data/raw/tiktok/2026-09-08/affiliate_orders.json`
- bestsellers: `/Users/VuIT/Desktop/APIClaude/integrations/tiktok_shop/data/raw/tiktok/2026-09-08/bestsellers.json`

No App Secret, auth_code, access_token, or refresh_token value appears anywhere in this report.