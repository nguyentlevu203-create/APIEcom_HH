#!/usr/bin/env python3
"""
One-time setup for the SEPARATE "Affiliate for order" app (App Category:
Affiliate Marketing Solution Management, Live Partner_id 2044772) — this
is a DIFFERENT app from the main production "Report Control Towner" app
(Live Partner_id 2044177) that the rest of this project uses. Storing
this key does NOT touch, read, or overwrite anything under the existing
`LIVE_PARTNER_KEY` / `ACCESS_TOKEN` / `REFRESH_TOKEN` keychain accounts.

Run this yourself, in your own Terminal.app window:

    cd /Users/VuIT/Desktop/APIClaude/integrations/shopee/auth
    python3 setup_partner_key_ams.py

You will get a hidden prompt (nothing typed is echoed to the screen).
Copy the Live API Partner Key from
https://open.shopee.com/console/app/241801 ("Live API Partner Key"
field, click the reveal/eye icon there) and paste it in. The key is
written directly to Keychain via the `keyring` package under:

    service  = HH_SHOPEE_OPEN_API
    account  = AMS_LIVE_PARTNER_KEY

This script never prints the key, never writes it to any file, and
never sends it anywhere.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

import keyring

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import KEYCHAIN_SERVICE  # noqa: E402

ACCOUNT_AMS_LIVE_PARTNER_KEY = "AMS_LIVE_PARTNER_KEY"


def has_secret(account: str) -> bool:
    return bool(keyring.get_password(KEYCHAIN_SERVICE, account))


def main() -> int:
    if has_secret(ACCOUNT_AMS_LIVE_PARTNER_KEY):
        print("An AMS Live Partner Key is already stored in Keychain.")
        answer = input("Overwrite it with a new value? [y/N]: ").strip().lower()
        if answer != "y":
            print("Kept existing key. AMS Live Partner Key stored: YES")
            return 0

    secret = getpass.getpass("AMS (Affiliate for order) Live API Partner Key (hidden): ").strip()
    if not secret:
        print("No key entered. Nothing stored.")
        return 1

    keyring.set_password(KEYCHAIN_SERVICE, ACCOUNT_AMS_LIVE_PARTNER_KEY, secret)
    del secret
    print("AMS Live Partner Key stored: YES")
    return 0


if __name__ == "__main__":
    sys.exit(main())
