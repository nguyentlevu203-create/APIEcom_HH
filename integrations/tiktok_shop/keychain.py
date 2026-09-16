"""
App Secret storage via the OS keychain (macOS Keychain on this machine),
through the cross-platform `keyring` package.

The App Secret NEVER touches disk in plaintext anywhere in this project —
not in source, not in .env, not in tokens.json, not in any report or log.
It lives only in the OS keychain and, transiently, in process memory.
"""
from __future__ import annotations

import getpass
from typing import Optional

import keyring

from config import KEYCHAIN_SERVICE


def get_app_secret(app_key: str) -> Optional[str]:
    return keyring.get_password(KEYCHAIN_SERVICE, app_key)


def set_app_secret(app_key: str, secret: str) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, app_key, secret)


def has_app_secret(app_key: str) -> bool:
    return bool(get_app_secret(app_key))


def get_app_secret_or_prompt(app_key: str) -> str:
    """Prefer the keychain; fall back to a hidden prompt if not stored yet."""
    secret = get_app_secret(app_key)
    if secret:
        return secret
    return getpass.getpass(
        f"App Secret not found in Keychain for {app_key}. Enter it now (hidden): "
    ).strip()
