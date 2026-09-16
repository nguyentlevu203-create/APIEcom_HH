# HH Shopee Open API — Connectivity Report

Generated: 2026-09-07 (Asia/Ho_Chi_Minh)
Checked by: browser audit of https://open.shopee.com/console (logged-in session), read-only.

## Account confirmed

- Developer Account: `digital@hhdistribution.vn`
- Developer Name: CÔNG TY CỔ PHẦN ĐẦU TƯ SẢN XUẤT VÀ XUẤT NHẬP KHẨU HOÀNG HÀ
- Username: `lepetitmarseillaisvn`
- Developer Type: Registered Business Seller (not Third-party Partner Platform)
- Country: Vietnam

This is the correct account type for a seller building software for its
own shop (as opposed to an ISV serving multiple third-party sellers).

## SHOPEE OPEN API
---------------
- App Registered: **NO**
- App Approved: N/A
- Environment: N/A
- Partner ID: N/A
- Partner Key: N/A (nothing to store — no app exists)
- Shop Authorized: N/A
- Shop ID: N/A
- Access Token: N/A
- Refresh Token: N/A
- Signing: N/A (not verified against docs yet — blocked on app creation)
- Shop Info API: NOT TESTED
- Orders API: NOT TESTED
- Order Detail API: NOT TESTED
- Payment API: NOT TESTED
- Product API: NOT TESTED
- Returns API: NOT TESTED
- Push: NOT CONFIGURED (Push Mechanism page shows "No data" — nothing to configure without an app)
- Security: N/A (no credential exists yet to leak)

## Evidence

- `https://open.shopee.com/console/app` → "Haven't created an application? Click Create App to launch your first application." — App List is empty.
- `https://open.shopee.com/console/push` → Push Mechanism table shows "No data" for App Information / Live Push / Live Push Number / Success Rate / Status.
- No changes were made: no app was created, no form was submitted, no config was touched. (A stray click briefly opened the "Create App" form; it was closed via Cancel without entering any data.)

## Overall: **API NOT REGISTERED**

## Missing steps, in order (none of these were performed — human decision required)

1. In `App List`, click **"Create a APP"** and submit App Category, App Name,
   Description, and App Logo.
2. Wait for Shopee's app approval (if required for this app type).
3. Retrieve **Partner ID** and **Partner Key** from the approved app.
   Partner Key must go straight into the macOS Keychain via a
   `setup_app_secret.py`-style script (to be built once an app exists) —
   never pasted into chat.
4. Run the **Shop Authorization** flow to authorize the Le Petit
   Marseillais Vietnam shop against this app, producing `shop_id`,
   `access_token`, and `refresh_token`.

Only after step 4 (state: APP + SHOP AUTHORIZATION READY) can this
project proceed to:
- re-verify the official Shopee Open Platform v2 docs (production host,
  HMAC signing algorithm, token refresh flow) at
  `https://open.shopee.com/documents`,
- build `integrations/shopee/` (client, token store, and the 5 read-only
  connectivity tests: Shop Info, Orders, Payment/Escrow, Product, Returns),
  mirroring the security model already in place for
  `integrations/tiktok_shop/` (Keychain-only secret, 0600 token file,
  hard-coded read-only allowlist, no credential logging).

No local code was created for this integration yet, since there is
nothing to authenticate with.
