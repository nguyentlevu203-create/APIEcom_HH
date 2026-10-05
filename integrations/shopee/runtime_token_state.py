"""
P11 RECOVERY D — runner-local rotating token state, shared by every
subprocess of one GitHub Actions job.

Problem (run 37262150230): each Shopee worker subprocess starts from the
job-start environment, so SHOPEE_TOKEN_STATE_JSON /
SHOPEE_AMS_TOKEN_STATE_JSON always hold the bundle provisioned at job
start. A refresh in worker A updated only A's own os.environ and the
GitHub Secret; worker B still saw the old (expired) access token and
refreshed again with the ORIGINAL refresh token — once per Shopee domain.

This module keeps the latest rotated bundle in one 0600 file per bundle
under $HH_TOKEN_STATE_DIR (provisioned by the workflow inside
$RUNNER_TEMP, removed `if: always()`), written atomically. GitHub Actions
only: on the local Mac nothing here is ever read or written, Keychain
behavior is unchanged. Never logs token values.

Read contract: a missing or invalid file (unparseable, not an object, any
required field empty) is ignored and the caller falls back to the env
bundle, exactly as before this module existed. A configured directory
that is inside the repository is refused (read: ignored, write: raises).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

STATE_DIR_ENV = "HH_TOKEN_STATE_DIR"
REQUIRED_KEYS = ("access_token", "refresh_token", "access_token_expire_at", "refresh_token_expire_at")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class RuntimeTokenStateError(RuntimeError):
    """Raised only on write — the message never contains token values."""


def _running_in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _state_dir() -> Optional[Path]:
    if not _running_in_github_actions():
        return None
    raw = os.environ.get(STATE_DIR_ENV)
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        return None
    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        return None
    return resolved


def _state_path(bundle_name: str) -> Optional[Path]:
    directory = _state_dir()
    return directory / f"{bundle_name}.json" if directory else None


def is_enabled() -> bool:
    return _state_dir() is not None


def read_state(bundle_name: str) -> Optional[dict]:
    path = _state_path(bundle_name)
    if path is None or not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(parsed, dict) or any(not parsed.get(k) for k in REQUIRED_KEYS):
        return None
    return parsed


def write_state(bundle_name: str, state_json: str) -> bool:
    """Atomically replace the runner-local bundle (0600). Returns False when
    not enabled (local Mac, or no directory configured); raises
    RuntimeTokenStateError if enabled but the write fails."""
    if _running_in_github_actions() and os.environ.get(STATE_DIR_ENV) and _state_dir() is None:
        raise RuntimeTokenStateError(f"{STATE_DIR_ENV} must be an absolute path outside the repository")
    path = _state_path(bundle_name)
    if path is None:
        return False
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{bundle_name}.", dir=str(path.parent))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(state_json)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError as exc:
        raise RuntimeTokenStateError(f"runner-local token state write failed for {bundle_name} ({type(exc).__name__})") from None
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return True
