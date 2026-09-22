#!/usr/bin/env python3
"""P11-QUINQUE Q1 — the one shared Neon writer-URL resolver for every
production GOLD script under scripts/gold/. Centralized so no caller
re-implements (and risks drifting from) the established credential
precedence already proven in pipelines/incr_common.py::get_db_conn():

  1. HH_ETL_WRITER_DATABASE_URL (GitHub Secret, set in the GitHub
     Actions workflow — the cloud-runner path, no OS Keychain there).
  2. HH_ECOM_DB_URL_OVERRIDE (Phase 6B rehearsal only — redirects to an
     isolated Neon branch; unset in the normal/production path).
  3. macOS Keychain (service HH_ECOM_NEON, account
     hh_etl_writer_database_url) — the local Mac fallback, unchanged.

Deliberately does NOT import from pipelines/ — scripts/gold/ stays a
self-contained directory (same reasoning pipelines/incr_common.py's own
docstring gives for why Shopee/TikTok integration code isn't imported
across platform boundaries: keep the dependency graph simple and avoid
sys.path surprises for whichever directory ends up on sys.path first).
"""
from __future__ import annotations

import os

import keyring


def resolve_writer_url() -> str:
    """Never logs or returns anything but the URL itself — callers must
    not print it and should `del` their local reference once connected,
    matching every other credential-resolution site in this codebase."""
    url = os.environ.get("HH_ETL_WRITER_DATABASE_URL")
    if not url:
        url = os.environ.get("HH_ECOM_DB_URL_OVERRIDE") or keyring.get_password(
            "HH_ECOM_NEON", "hh_etl_writer_database_url",
        )
    if not url:
        raise RuntimeError(
            "hh_etl_writer_database_url missing: no HH_ETL_WRITER_DATABASE_URL or "
            "HH_ECOM_DB_URL_OVERRIDE env var set, and the macOS Keychain lookup "
            "(service=HH_ECOM_NEON, account=hh_etl_writer_database_url) also failed"
        )
    return url
