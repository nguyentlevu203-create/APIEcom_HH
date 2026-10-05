"""
Repo-wide pytest safety (P11 RECOVERY D2). Loaded for every test run from
any subdirectory (pytest.ini at the repo root makes this the rootdir).

Unless HH_ALLOW_LIVE_API_TESTS=1:
  * Shopee / TikTok / GitHub API / Neon hostnames cannot be resolved, in
    this process and in every Python subprocess (testing/live_api_guard);
  * the production DB URLs are forced to an unreachable address, so a
    missed monkeypatch can never fall through to the Keychain credentials.
"""
import os
import sys
from pathlib import Path

_GUARD_DIR = Path(__file__).resolve().parent / "testing" / "live_api_guard"
sys.path.insert(0, str(_GUARD_DIR))
import hh_live_api_guard  # noqa: E402

BLOCKED_DB_URL = "postgresql://blocked@127.0.0.1:1/blocked"

if hh_live_api_guard.should_deny():
    os.environ[hh_live_api_guard.GUARD_ENV] = "deny"
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_GUARD_DIR), os.environ.get("PYTHONPATH")]))
    os.environ["HH_ETL_WRITER_DATABASE_URL"] = BLOCKED_DB_URL
    os.environ["HH_NEONDB_OWNER_DATABASE_URL"] = BLOCKED_DB_URL
    hh_live_api_guard.install()
