"""
P11 RECOVERY A — durable persistence of a refreshed TikTok token bundle.

Local Mac: nothing to do here — token_store.save_tokens() already wrote
tokens.json, which is the durable store on that machine.

GitHub Actions: tokens.json is provisioned from the TIKTOK_TOKENS_JSON
secret at job start and deleted at job end, so a refresh written only to
that file is lost and the next run starts from the stale secret. This
module writes the FULL merged bundle back to TIKTOK_TOKENS_JSON as one
atomic secret update (never several partial secrets), using the same
dedicated GH_SECRETS_WRITER_TOKEN and sealed-box writer as Shopee's
persist_rotating_state(). Token values are never printed or logged.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

SECRET_NAME = "TIKTOK_TOKENS_JSON"
FAILURE_CODE = "TOKEN_STATE_PERSISTENCE_FAILED"
REQUIRED_KEYS = ("access_token", "refresh_token", "access_token_expire_in", "refresh_token_expire_in")

_WRITER_PATH = Path(__file__).resolve().parent.parent / "shopee" / "github_secrets_writer.py"


class TokenStatePersistenceFailed(RuntimeError):
    """TikTok already issued the new token by the time this is raised —
    the run must fail loudly rather than strand the next run on a stale
    secret. The message always starts with FAILURE_CODE so the production
    cycle can promote it by name."""


def _running_in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _load_writer():
    # Loaded by file path, not via sys.path: integrations/shopee and
    # integrations/tiktok_shop both have a top-level config.py, and putting
    # the Shopee directory on sys.path here would shadow TikTok's config.
    spec = importlib.util.spec_from_file_location("hh_github_secrets_writer", _WRITER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_bundle(bundle: dict[str, Any]) -> list[str]:
    """Schema problems (field names only, never values)."""
    problems = [f"missing:{k}" for k in REQUIRED_KEYS if not bundle.get(k)]
    for k in ("access_token_expire_in", "refresh_token_expire_in"):
        if bundle.get(k) and not str(bundle[k]).isdigit():
            problems.append(f"not_epoch:{k}")
    return problems


def persist_refreshed_bundle(bundle: dict[str, Any]) -> bool:
    """Returns False when not in GitHub Actions (no-op), True after a
    successful durable write. Raises TokenStatePersistenceFailed otherwise."""
    if not _running_in_github_actions():
        return False

    problems = validate_bundle(bundle)
    if problems:
        raise TokenStatePersistenceFailed(f"{FAILURE_CODE}: refreshed bundle failed schema check {problems}")

    repo = os.environ.get("GITHUB_REPOSITORY")
    writer_token = os.environ.get("GH_SECRETS_WRITER_TOKEN")
    if not repo or not writer_token:
        raise TokenStatePersistenceFailed(
            f"{FAILURE_CODE}: GITHUB_REPOSITORY or GH_SECRETS_WRITER_TOKEN missing from the "
            "GitHub Actions environment"
        )

    try:
        writer = _load_writer()
        payload = json.dumps(bundle)
        ok = writer.put_secret_with_retry(SECRET_NAME, payload, repo, writer_token)
        del payload
    except Exception as exc:  # noqa: BLE001 — class name only, never the message (may echo request data)
        raise TokenStatePersistenceFailed(f"{FAILURE_CODE}: {type(exc).__name__} while writing {SECRET_NAME}") from None
    if not ok:
        raise TokenStatePersistenceFailed(f"{FAILURE_CODE}: write to {SECRET_NAME} failed after retries")
    return True
