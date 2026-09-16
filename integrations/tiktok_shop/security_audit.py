"""Phase 12: security audit checks — git tracking, .gitignore, file perms."""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
REQUIRED_GITIGNORE_PATTERNS = [
    "tokens.json",
    "shop_info.json",
    ".env",
    "*.secret",
    "credentials*",
    "data/raw/",
    "__pycache__/",
    ".venv/",
]
SENSITIVE_FILENAMES = ("tokens.json", "shop_info.json")


def _run_git(args: list[str], cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except FileNotFoundError:
        return 127, "git not installed"


def audit() -> dict[str, Any]:
    result: dict[str, Any] = {
        "is_git_repo": False,
        "secret_file_ever_tracked": False,
        "incident": False,
        "incident_files": [],
        "gitignore_ok": False,
        "gitignore_missing": [],
        "file_perms_ok": True,
        "file_perms_detail": {},
        "notes": [],
    }

    # Walk up to find a repo root (this project may live inside a larger one).
    repo_root = None
    for d in [BASE_DIR, *BASE_DIR.parents]:
        if (d / ".git").exists():
            repo_root = d
            break

    if repo_root is None:
        result["notes"].append("No .git directory found in this project or any parent — not a git repository, so no secret file could ever have been tracked.")
    else:
        result["is_git_repo"] = True
        code, out = _run_git(["log", "--all", "--pretty=format:", "--name-only"], repo_root)
        tracked_now_code, tracked_now = _run_git(["ls-files"], repo_root)
        history_hits = [f for f in SENSITIVE_FILENAMES if f in out]
        current_hits = [f for f in SENSITIVE_FILENAMES if f in tracked_now.splitlines()]
        hits = sorted(set(history_hits) | set(current_hits))
        if hits:
            result["secret_file_ever_tracked"] = True
            result["incident"] = True
            result["incident_files"] = hits

    gitignore_path = BASE_DIR / ".gitignore"
    if gitignore_path.exists():
        content = gitignore_path.read_text(encoding="utf-8")
        missing = [p for p in REQUIRED_GITIGNORE_PATTERNS if p not in content]
        result["gitignore_missing"] = missing
        result["gitignore_ok"] = not missing
    else:
        result["gitignore_missing"] = REQUIRED_GITIGNORE_PATTERNS
        result["gitignore_ok"] = False

    for fname in SENSITIVE_FILENAMES:
        fpath = BASE_DIR / fname
        if fpath.exists():
            mode = stat.S_IMODE(fpath.stat().st_mode)
            ok = mode == 0o600
            result["file_perms_detail"][fname] = oct(mode)
            result["file_perms_ok"] = result["file_perms_ok"] and ok

    return result
