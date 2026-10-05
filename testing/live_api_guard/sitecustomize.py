# P11 RECOVERY D2 — loaded automatically by every Python subprocess started
# while the repo-root conftest.py has put this directory on PYTHONPATH.
# Installs the live-API guard when the test run asked for it.
import os

if os.environ.get("HH_LIVE_API_GUARD") == "deny":
    import hh_live_api_guard

    hh_live_api_guard.install()
