"""
Shared bootstrap + helpers for the FIRST END-TO-END REPORTING PILOT.

HARD RULE: this module does not reimplement auth. It reuses the existing,
already-PASSing mechanism in ../token_store.py, ../keychain.py,
../refresh_token.py, ../tiktok_client.py exactly as run_all.py does.
If tokens are unusable, this stops safely — it never re-authorizes and
never invents state.

No App Secret / access_token / refresh_token is ever written to any file
under pilot_reporting/. Raw API responses are redacted of those keys
before being written to disk (same redaction rules as read_tests.py).
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

INTEGRATION_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INTEGRATION_DIR))

import refresh_token as refresh_mod  # noqa: E402
from config import APP_KEY  # noqa: E402
from keychain import get_app_secret, has_app_secret  # noqa: E402
from tiktok_client import TikTokShopClient  # noqa: E402
from token_store import (  # noqa: E402
    access_token_needs_refresh,
    load_shop_info,
    load_tokens,
    refresh_token_expired,
)

PILOT_DIR = Path(__file__).resolve().parent
RAW_DIR = PILOT_DIR / "raw"
NORMALIZED_DIR = PILOT_DIR / "normalized"
REPORTS_DIR = PILOT_DIR / "reports"
LOGS_DIR = PILOT_DIR / "logs"

for d in (RAW_DIR, NORMALIZED_DIR, REPORTS_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

_SENSITIVE_KEYS = {
    "access_token", "refresh_token", "app_secret", "auth_code", "token", "sign",
}


class PilotStopError(RuntimeError):
    """Raised to stop the pilot safely (auth not ready, baseline not ready).
    Never caught-and-ignored; main() prints it and exits non-zero."""


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("***REDACTED***" if k.lower() in _SENSITIVE_KEYS else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


@dataclass
class Session:
    client: TikTokShopClient
    access_token: str
    shop_info: dict[str, Any]
    granted_scopes: set[str]


def bootstrap_session() -> Session:
    """Reuse the exact working auth chain from run_all.py. Raises
    PilotStopError (not SystemExit) on anything that should stop the pilot
    — callers decide how to report that."""
    tokens = load_tokens()
    if not tokens:
        raise PilotStopError("No tokens.json found — run authorize.py first.")

    app_key = tokens.get("app_key") or APP_KEY

    if not has_app_secret(app_key):
        raise PilotStopError("READY FOR APP SECRET INPUT — run setup_app_secret.py first.")

    if refresh_token_expired(tokens):
        raise PilotStopError("REAUTHORIZATION REQUIRED — refresh_token has expired.")

    app_secret = get_app_secret(app_key)

    if access_token_needs_refresh(tokens):
        tokens = refresh_mod.do_refresh(app_key, app_secret, tokens["refresh_token"])
        ok, problems = refresh_mod.validate_post_refresh(tokens)
        if not ok:
            raise PilotStopError(f"Post-refresh validation failed: {problems}")

    shop_info = load_shop_info()
    if not shop_info:
        raise PilotStopError("No shop_info.json found — run run_all.py once first.")

    granted_scopes = set(tokens.get("granted_scopes") or [])
    client = TikTokShopClient(app_key=app_key, app_secret=app_secret)
    return Session(
        client=client,
        access_token=tokens["access_token"],
        shop_info=shop_info,
        granted_scopes=granted_scopes,
    )


def write_raw(
    report_date: str,
    filename: str,
    *,
    endpoint: str,
    api_version: str,
    shop_info: dict[str, Any],
    request_time_utc: str,
    response,  # tiktok_client.APIResponse
    page_number: Optional[int] = None,
    next_page_token: Optional[str] = None,
    extra_meta: Optional[dict[str, Any]] = None,
) -> Path:
    """Write one raw API response snapshot with the metadata envelope
    required by the pilot spec (Section 4). Never includes any secret."""
    out_dir = RAW_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    payload = {
        "source": "TikTok Shop Open API",
        "endpoint": endpoint,
        "api_version": api_version,
        "shop": {
            "shop_id": shop_info.get("shop_id"),
            "shop_name": shop_info.get("shop_name"),
            "shop_region": shop_info.get("region"),
            "shop_cipher_present": bool(shop_info.get("shop_cipher")),
        },
        "report_date": report_date,
        "request_time_utc": request_time_utc,
        "request_id": response.request_id,
        "page_number": page_number,
        "next_page_token": next_page_token,
        "response_code": response.code,
        "response_message": response.message,
        "http_status": response.http_status,
        "data": redact(response.data),
    }
    if extra_meta:
        payload.update(extra_meta)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
    return path


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
