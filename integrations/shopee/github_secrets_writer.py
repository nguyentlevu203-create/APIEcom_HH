"""
Minimal GitHub Actions repository-secrets REST client.

Used only to durably persist Shopee's rotated (access_token, refresh_token,
*_expire_at) state from inside a GitHub Actions runner, where there is no
OS keychain to write back to (see keychain.py:persist_rotating_state).
Never used on the local Mac path.

Encryption follows GitHub's documented method for the Actions secrets API
(libsodium sealed-box via PyNaCl):
https://docs.github.com/en/rest/actions/secrets
"""
from __future__ import annotations

import base64
import time
from typing import Optional

import requests
from nacl import encoding, public

API_ROOT = "https://api.github.com"


class GitHubSecretsError(RuntimeError):
    """A GitHub Actions secret create/update/delete/read call failed."""


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _encrypt(public_key_b64: str, secret_value: str) -> str:
    pk = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pk)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def _get_public_key(repo: str, token: str) -> tuple[str, str]:
    resp = requests.get(
        f"{API_ROOT}/repos/{repo}/actions/secrets/public-key",
        headers=_headers(token),
        timeout=15,
    )
    if resp.status_code != 200:
        raise GitHubSecretsError(f"public-key fetch failed (http={resp.status_code})")
    data = resp.json()
    return data["key_id"], data["key"]


def put_secret(name: str, value: str, repo: str, token: str) -> None:
    """Create or update a repository Actions secret. Never logs `value`."""
    key_id, key = _get_public_key(repo, token)
    encrypted_value = _encrypt(key, value)
    resp = requests.put(
        f"{API_ROOT}/repos/{repo}/actions/secrets/{name}",
        headers=_headers(token),
        json={"encrypted_value": encrypted_value, "key_id": key_id},
        timeout=15,
    )
    if resp.status_code not in (201, 204):
        raise GitHubSecretsError(f"secret write failed for {name} (http={resp.status_code})")


def get_secret_metadata(name: str, repo: str, token: str) -> Optional[dict]:
    """Return {'created_at', 'updated_at'}, or None if absent. GitHub's API
    never exposes the value itself, so there is nothing to leak here."""
    resp = requests.get(
        f"{API_ROOT}/repos/{repo}/actions/secrets/{name}",
        headers=_headers(token),
        timeout=15,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise GitHubSecretsError(f"secret metadata fetch failed for {name} (http={resp.status_code})")
    data = resp.json()
    return {"created_at": data.get("created_at"), "updated_at": data.get("updated_at")}


def delete_secret(name: str, repo: str, token: str) -> None:
    resp = requests.delete(
        f"{API_ROOT}/repos/{repo}/actions/secrets/{name}",
        headers=_headers(token),
        timeout=15,
    )
    if resp.status_code not in (204, 404):
        raise GitHubSecretsError(f"secret delete failed for {name} (http={resp.status_code})")


def put_secret_with_retry(
    name: str,
    value: str,
    repo: str,
    token: str,
    attempts: int = 3,
    base_delay: float = 2.0,
) -> bool:
    """Retry with exponential backoff. Returns True on success, False if every
    attempt failed — never raises, since the caller (keychain.py) decides how
    a persistence failure should be treated. Never logs `value`."""
    for attempt in range(1, attempts + 1):
        try:
            put_secret(name, value, repo, token)
            return True
        except (GitHubSecretsError, requests.RequestException):
            if attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
    return False
