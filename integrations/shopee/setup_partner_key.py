#!/usr/bin/env python3
"""
One-time setup: store the LIVE API Partner Key in the macOS Keychain so
every other script in this project can read it without it ever being
pasted into a chat, a file, or an environment variable.

Run this yourself, in your own Terminal.app window:

    cd /Users/VuIT/Desktop/APIClaude/integrations/shopee
    python3 setup_partner_key.py

You will get a hidden prompt (nothing typed is echoed to the screen). Copy
the Live API Partner Key from https://open.shopee.com/console/app/241206
("Live API Partner Key" field, click the reveal/eye icon there) and paste
it in. The key is written directly to Keychain via the `keyring` package
under:

    service  = HH_SHOPEE_OPEN_API
    account  = LIVE_PARTNER_KEY

This script never prints the key, never writes it to any file, and never
sends it anywhere.
"""
from __future__ import annotations

import getpass
import sys

from keychain import ACCOUNT_LIVE_PARTNER_KEY, has_secret, set_secret


def main() -> int:
    if has_secret(ACCOUNT_LIVE_PARTNER_KEY):
        print("A Live Partner Key is already stored in Keychain.")
        answer = input("Overwrite it with a new value? [y/N]: ").strip().lower()
        if answer != "y":
            print("Kept existing key. Live Partner Key stored: YES")
            return 0

    secret = getpass.getpass("Live API Partner Key (hidden): ").strip()
    if not secret:
        print("Empty input — nothing stored. Live Partner Key stored: NO")
        return 1

    set_secret(ACCOUNT_LIVE_PARTNER_KEY, secret)
    del secret  # drop the local reference as soon as possible

    stored = has_secret(ACCOUNT_LIVE_PARTNER_KEY)
    print(f"Live Partner Key stored: {'YES' if stored else 'NO'}")
    return 0 if stored else 1


if __name__ == "__main__":
    sys.exit(main())
