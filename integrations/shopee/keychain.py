"""
Secure storage via the OS keychain (macOS Keychain on this machine), through
the cross-platform `keyring` package.

The Live Partner Key, access_token, and refresh_token NEVER touch disk in
plaintext anywhere in this project — not in source, not in .env, not in any
JSON file, not in any report or log. They live only in the OS keychain and,
transiently, in process memory during a request.
"""
from __future__ import annotations

import json
import os
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

# The four fields that rotate together every Shopee token refresh (as
# opposed to LIVE_PARTNER_KEY/SHOP_ID/PARTNER_ID, which are static). Their
# JSON key names inside the bundled SHOPEE_TOKEN_STATE_JSON secret.
_ROTATING_JSON_KEYS = {
    ACCOUNT_ACCESS_TOKEN: "access_token",
    ACCOUNT_REFRESH_TOKEN: "refresh_token",
    ACCOUNT_ACCESS_TOKEN_EXPIRE_AT: "access_token_expire_at",
    ACCOUNT_REFRESH_TOKEN_EXPIRE_AT: "refresh_token_expire_at",
}


class CredentialPersistenceCriticalFailure(RuntimeError):
    """Shopee's refresh endpoint already rotated the credential server-side,
    but the new state could not be durably persisted anywhere the next run
    can read it. Must be treated as a hard failure, never swallowed — the
    caller should exit non-zero rather than continue as if healthy."""


def _bundled_rotating_state() -> Optional[dict]:
    """Parse SHOPEE_TOKEN_STATE_JSON if present. Malformed or non-object
    content is treated the same as absent (falls through to legacy/keychain)
    rather than raising, since a bad env var must never crash the read path."""
    raw = os.environ.get("SHOPEE_TOKEN_STATE_JSON")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _running_in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def get_secret(account: str) -> Optional[str]:
    # Precedence for the four rotating fields: (1) bundled
    # SHOPEE_TOKEN_STATE_JSON — the freshest known-good rotating state,
    # (2) legacy individual SHOPEE_<account> env var (backward-compat
    # bootstrap), (3) local Keychain. A present-but-empty/null field in the
    # bundle is treated as absent, not as a valid value — it falls through
    # rather than silently standing in for a different credential.
    json_key = _ROTATING_JSON_KEYS.get(account)
    if json_key is not None:
        bundled = _bundled_rotating_state()
        if bundled is not None:
            value = bundled.get(json_key)
            if value:
                return str(value)

    # Phase 6C scheduler-migration — GitHub Actions runners have no macOS
    # Keychain. An env var named SHOPEE_<account> (set from a GitHub
    # Secret) takes precedence when present; unset on the Mac local/
    # production path, so behavior there is unchanged.
    env_value = os.environ.get(f"SHOPEE_{account}")
    if env_value:
        return env_value
    return keyring.get_password(KEYCHAIN_SERVICE, account)


def set_secret(account: str, value: str) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, account, value)


def persist_rotating_state(
    access_token: str,
    refresh_token: str,
    access_token_expire_at: str,
    refresh_token_expire_at: str,
) -> None:
    """Durably persist a freshly-rotated Shopee token state.

    Local Mac: unchanged — four Keychain entries, exactly as before.

    GitHub Actions: there is no Keychain to write to, so the complete new
    state is bundled into one SHOPEE_TOKEN_STATE_JSON secret (a single
    atomic snapshot — never four independent partial writes) via the
    GitHub REST API, with retry/backoff. Raises
    CredentialPersistenceCriticalFailure if that fails after retries,
    since Shopee has already rotated the credential server-side by the
    time this is called — silently continuing would strand the next run.
    """
    if not _running_in_github_actions():
        set_secret(ACCOUNT_ACCESS_TOKEN, access_token)
        set_secret(ACCOUNT_REFRESH_TOKEN, refresh_token)
        set_secret(ACCOUNT_ACCESS_TOKEN_EXPIRE_AT, access_token_expire_at)
        set_secret(ACCOUNT_REFRESH_TOKEN_EXPIRE_AT, refresh_token_expire_at)
        return

    from github_secrets_writer import put_secret_with_retry

    repo = os.environ.get("GITHUB_REPOSITORY")
    writer_token = os.environ.get("GH_SECRETS_WRITER_TOKEN")
    if not repo or not writer_token:
        raise CredentialPersistenceCriticalFailure(
            "GITHUB_REPOSITORY or GH_SECRETS_WRITER_TOKEN missing from the "
            "GitHub Actions environment — cannot durably persist rotated state"
        )

    state_json = json.dumps(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "access_token_expire_at": access_token_expire_at,
            "refresh_token_expire_at": refresh_token_expire_at,
        }
    )
    # Switch the current process to the new state immediately — every
    # subsequent get_secret() call in this run (including the one the
    # in-flight ShopeeClient request is about to make) must see the fresh
    # token regardless of whether the durable write below succeeds.
    os.environ["SHOPEE_TOKEN_STATE_JSON"] = state_json
    ok = put_secret_with_retry("SHOPEE_TOKEN_STATE_JSON", state_json, repo, writer_token)
    del state_json
    if not ok:
        raise CredentialPersistenceCriticalFailure(
            "durable persistence of rotated Shopee token state to "
            "GitHub Secrets failed after retries"
        )


def has_secret(account: str) -> bool:
    return bool(get_secret(account))


def delete_secret(account: str) -> None:
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, account)
    except keyring.errors.PasswordDeleteError:
        pass
