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

Read contract (P11 RECOVERY D2):
  * file ABSENT -> None, caller falls back to the env bundle (first
    worker of the job, nothing refreshed yet);
  * file PRESENT but unreadable, unparseable, not an object, any required
    field empty, not owned by this user, or group/other-accessible ->
    RuntimeTokenStateError (fail closed: never silently fall back to the
    stale job-start env bundle, which may hold an already-rotated token);
  * HH_TOKEN_STATE_DIR set on GitHub Actions but relative or inside the
    repository -> RuntimeTokenStateError on read and write.
Error messages carry bundle names and error classes only, never values.
"""
from __future__ import annotations

import json
import os
import stat
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
    resolved = path.resolve() if path.is_absolute() else None
    if resolved is None or resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise RuntimeTokenStateError(f"{STATE_DIR_ENV} must be an absolute path outside the repository")
    return resolved


def _state_path(bundle_name: str) -> Optional[Path]:
    directory = _state_dir()
    return directory / f"{bundle_name}.json" if directory else None


def is_enabled() -> bool:
    return _state_dir() is not None


def read_state(bundle_name: str) -> Optional[dict]:
    path = _state_path(bundle_name)
    if path is None or not os.path.lexists(path):
        return None
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeTokenStateError(f"runner-local token state for {bundle_name} is not a regular file")
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise RuntimeTokenStateError(f"runner-local token state for {bundle_name} has unsafe ownership/permissions")
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except RuntimeTokenStateError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeTokenStateError(f"runner-local token state for {bundle_name} is unreadable ({type(exc).__name__})") from None
    if not isinstance(parsed, dict) or any(not parsed.get(k) for k in REQUIRED_KEYS):
        raise RuntimeTokenStateError(f"runner-local token state for {bundle_name} failed schema validation")
    return parsed


def write_state(bundle_name: str, state_json: str) -> bool:
    """Atomically replace the runner-local bundle (0600). Returns False when
    not enabled (local Mac, or no directory configured); raises
    RuntimeTokenStateError if enabled but the write fails."""
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
