"""
Secure storage via the OS keychain (macOS Keychain on this machine), through
the cross-platform `keyring` package.

The Live Partner Key, access_token, and refresh_token NEVER touch disk in
plaintext anywhere in this project — not in source, not in .env, not in any
JSON file, not in any report or log. They live only in the OS keychain and,
transiently, in process memory during a request.
"""
from __future__ import annotations

from typing import Optional

import keyring

from config import KEYCHAIN_SERVICE

# Keychain account names (not secret themselves — just labels)
ACCOUNT_LIVE_PARTNER_KEY = "LIVE_PARTNER_KEY"
ACCOUNT_ACCESS_TOKEN = "ACCESS_TOKEN"
ACCOUNT_REFRESH_TOKEN = "REFRESH_TOKEN"
ACCOUNT_SHOP_ID = "SHOP_ID"
ACCOUNT_PARTNER_ID = "PARTNER_ID"
ACCOUNT_ACCESS_TOKEN_EXPIRE_AT = "ACCESS_TOKEN_EXPIRE_AT"
ACCOUNT_REFRESH_TOKEN_EXPIRE_AT = "REFRESH_TOKEN_EXPIRE_AT"


def get_secret(account: str) -> Optional[str]:
    return keyring.get_password(KEYCHAIN_SERVICE, account)


def set_secret(account: str, value: str) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, account, value)


def has_secret(account: str) -> bool:
    return bool(get_secret(account))


def delete_secret(account: str) -> None:
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, account)
    except keyring.errors.PasswordDeleteError:
        pass
