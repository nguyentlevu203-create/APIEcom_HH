"""
P11 RECOVERY D2 — test-only network guard.

2026-10-05: a stale monkeypatch let a unit test spawn real TikTok workers
from the Mac (real API calls, a local token refresh, production DB
connections). Under pytest, this guard refuses to resolve any Shopee /
TikTok / GitHub API / Neon hostname, in the pytest process itself and in
every Python subprocess (installed via sitecustomize.py on PYTHONPATH,
see the repo-root conftest.py). It fails at name resolution — before any
socket is created or connected — with a deterministic LiveApiBlocked
error that is NOT an OSError, so HTTP retry/backoff loops never retry it.

Live access needs an explicit HH_ALLOW_LIVE_API_TESTS=1. Production and
GitHub workflow runs never load this module (it is only on PYTHONPATH when
the test conftest puts it there).
"""
from __future__ import annotations

import os
import socket

GUARD_ENV = "HH_LIVE_API_GUARD"
OPT_IN_ENV = "HH_ALLOW_LIVE_API_TESTS"
BLOCKED_SUFFIXES = (
    "shopeemobile.com", "shopee.com", "shopee.vn", "shopee.co.id", "shopee.sg",
    "tiktokglobalshop.com", "tiktok-shops.com", "tiktokshop.com", "tiktokshops.com",
    "api.github.com", "neon.tech",
)

_original_getaddrinfo = socket.getaddrinfo


class LiveApiBlocked(RuntimeError):
    """Raised instead of resolving a live API host during tests."""


def is_blocked_host(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        return False
    host = host.rstrip(".").lower()
    return any(host == s or host.endswith("." + s) for s in BLOCKED_SUFFIXES)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if is_blocked_host(host):
        raise LiveApiBlocked(f"HH_LIVE_API_BLOCKED: {host} (set {OPT_IN_ENV}=1 to allow live API tests)")
    return _original_getaddrinfo(host, *args, **kwargs)


def install() -> None:
    socket.getaddrinfo = _guarded_getaddrinfo


def is_installed() -> bool:
    return socket.getaddrinfo is _guarded_getaddrinfo


def should_deny(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get(OPT_IN_ENV) != "1"
