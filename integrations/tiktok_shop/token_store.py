"""
Local, file-based storage for TikTok Shop tokens and shop identity.

Both files created here contain operational data that must stay out of
Git (see the .gitignore in this folder) and off shared machines:
- tokens.json    : access_token, refresh_token, their expiry, seller info
- shop_info.json : shop_id, shop_cipher, shop_code, shop_name, region

Neither file ever contains the App Secret.

IMPORTANT — token expiry semantics: per the official TikTok Shop docs,
`access_token_expire_in` and `refresh_token_expire_in` in the token
response are ABSOLUTE UNIX EPOCH TIMESTAMPS at which the token expires —
NOT a duration in seconds, despite the "_in" naming. This module treats
them as the single source of truth and never adds `now` to them, and
never caches a separately-computed "*_expire_at" field that could go
stale relative to the raw value.
"""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Optional

from config import REFRESH_BUFFER_SECONDS

BASE_DIR = Path(__file__).resolve().parent
TOKENS_PATH = BASE_DIR / "tokens.json"
SHOP_INFO_PATH = BASE_DIR / "shop_info.json"


def _write_json_private_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically (temp file + rename) with 0600 permissions,
    so a crash mid-write can never leave a corrupt or world-readable file."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600: owner read/write only
    os.replace(tmp_path, path)  # atomic on POSIX


def save_tokens(data: dict[str, Any]) -> None:
    record = dict(data)
    # Drop any legacy/derived expiry fields from a previous buggy version —
    # access_token_expire_in / refresh_token_expire_in (as returned by
    # TikTok) are the only source of truth for expiry.
    record.pop("access_token_expire_at", None)
    record.pop("refresh_token_expire_at", None)
    record["saved_at"] = int(time.time())
    _write_json_private_atomic(TOKENS_PATH, record)


def load_tokens() -> Optional[dict[str, Any]]:
    if not TOKENS_PATH.exists():
        return None
    return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))


def save_shop_info(shop: dict[str, Any]) -> None:
    _write_json_private_atomic(SHOP_INFO_PATH, shop)


def load_shop_info() -> Optional[dict[str, Any]]:
    if not SHOP_INFO_PATH.exists():
        return None
    return json.loads(SHOP_INFO_PATH.read_text(encoding="utf-8"))


def seconds_remaining(tokens: dict[str, Any], key: str) -> int:
    """`key` is 'access_token_expire_in' or 'refresh_token_expire_in' — both
    are absolute epoch timestamps, not durations."""
    return int(tokens.get(key, 0)) - int(time.time())


def access_token_needs_refresh(tokens: dict[str, Any]) -> bool:
    return seconds_remaining(tokens, "access_token_expire_in") <= REFRESH_BUFFER_SECONDS


def refresh_token_expired(tokens: dict[str, Any]) -> bool:
    return seconds_remaining(tokens, "refresh_token_expire_in") <= 0
