"""
P11 RECOVERY D2 — the test-run live-API guard (testing/live_api_guard,
repo-root conftest.py). No credentials, no network: blocked hosts fail at
name resolution, before any socket exists.

Run with: python3 -m pytest testing/test_live_api_guard.py -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import hh_live_api_guard as guard

ROOT = Path(__file__).resolve().parent.parent

_CHILD = textwrap.dedent("""
    import json, socket, sys
    import requests  # urllib3 opens one local IPv6 probe socket at import; count only what the requests do
    connects = []
    _orig_connect = socket.socket.connect
    socket.socket.connect = lambda self, addr: connects.append(addr) or _orig_connect(self, addr)
    created = []
    _orig_init = socket.socket.__init__
    def _init(self, *a, **k):
        created.append(1)
        _orig_init(self, *a, **k)
    socket.socket.__init__ = _init
    result = {}
    for url in ("https://partner.shopeemobile.com/api/v2/shop/get_shop_info",
                "https://open-api.tiktokglobalshop.com/order/202309/orders/search",
                "https://auth.tiktok-shops.com/api/v2/token/refresh"):
        try:
            requests.get(url, timeout=5)
            result[url] = "NOT_BLOCKED"
        except Exception as e:  # noqa: BLE001
            result[url] = type(e).__name__ + ":" + str(e)[:80]
    print(json.dumps({"result": result, "sockets_created": len(created), "connects": len(connects),
                      "guard_env": __import__("os").environ.get("HH_LIVE_API_GUARD")}))
""")


def _run_child(env):
    return subprocess.run([sys.executable, "-c", _CHILD], env=env, capture_output=True, text=True, timeout=60)


def test_guard_active_in_pytest_process():
    assert guard.is_installed()
    with pytest.raises(guard.LiveApiBlocked, match="HH_LIVE_API_BLOCKED"):
        __import__("socket").getaddrinfo("partner.shopeemobile.com", 443)
    assert os.environ["HH_ETL_WRITER_DATABASE_URL"].startswith("postgresql://blocked@127.0.0.1:1/")


def test_real_child_subprocess_inherits_block_before_any_socket():
    import json
    proc = _run_child(dict(os.environ))
    assert proc.returncode == 0, proc.stderr[-800:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["guard_env"] == "deny"
    for url, outcome in out["result"].items():
        assert "HH_LIVE_API_BLOCKED" in outcome, (url, outcome)
    assert out["sockets_created"] == 0 and out["connects"] == 0


def test_guard_error_is_not_an_oserror_so_http_retries_never_loop():
    assert not issubclass(guard.LiveApiBlocked, OSError)


def test_opt_in_is_separately_controlled():
    assert guard.should_deny({}) is True
    assert guard.should_deny({"HH_ALLOW_LIVE_API_TESTS": "0"}) is True
    assert guard.should_deny({"HH_ALLOW_LIVE_API_TESTS": "1"}) is False
    # A child started without the guard env (what an opted-in run, or any
    # production/workflow process, looks like) does not install it — checked
    # without making any network call.
    env = {k: v for k, v in os.environ.items() if k != "HH_LIVE_API_GUARD"}
    probe = "import socket, hh_live_api_guard as g; print(socket.getaddrinfo is g._guarded_getaddrinfo)"
    proc = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=30)
    assert proc.stdout.strip() == "False", proc.stderr[-500:]


def test_non_api_hosts_untouched_and_matching_is_suffix_exact():
    assert guard.is_blocked_host("open-api.tiktokglobalshop.com")
    assert guard.is_blocked_host("ep-cool-123.ap-southeast-1.aws.neon.tech")
    assert guard.is_blocked_host(b"api.github.com")
    assert not guard.is_blocked_host("127.0.0.1")
    assert not guard.is_blocked_host("notshopee.com.example")
    assert not guard.is_blocked_host("evilshopee.com")


def test_production_workflows_never_load_the_guard():
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        assert "live_api_guard" not in text and "HH_LIVE_API_GUARD" not in text, wf.name
