#!/usr/bin/env python3
"""
One-time setup: store the App Secret in the macOS Keychain so every other
script in this project can read it without ever asking you to paste it
into a chat, a file, or an environment variable.

Run this yourself, in your own Terminal.app window:

    cd /Users/VuIT/Desktop/APIClaude/integrations/tiktok_shop
    python3 setup_app_secret.py

You will get a hidden prompt (nothing typed is echoed to the screen). The
secret is written directly to Keychain via the `keyring` package under:

    service  = HH_TIKTOK_SHOP_OPEN_API
    account  = 6jh02dvvivnis   (the App Key)

This script never prints the secret, never writes it to any file, and
never sends it anywhere.
"""
from __future__ import annotations

import getpass
import sys

from config import APP_KEY
from keychain import has_app_secret, set_app_secret


def main() -> int:
    if has_app_secret(APP_KEY):
        print(f"A secret is already stored in Keychain for {APP_KEY}.")
        answer = input("Overwrite it with a new value? [y/N]: ").strip().lower()
        if answer != "y":
            print("Kept existing secret. App Secret stored: YES")
            return 0

    secret = getpass.getpass("App Secret (hidden): ").strip()
    if not secret:
        print("Empty input — nothing stored. App Secret stored: NO")
        return 1

    set_app_secret(APP_KEY, secret)
    del secret  # drop the local reference as soon as possible

    stored = has_app_secret(APP_KEY)
    print(f"App Secret stored: {'YES' if stored else 'NO'}")
    return 0 if stored else 1


if __name__ == "__main__":
    sys.exit(main())
