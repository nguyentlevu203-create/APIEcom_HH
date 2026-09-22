"""
P11-QUINQUE Q4 — regression tests for scripts/gold/_gold_db.py's Neon
credential resolution: the exact precedence that fixed the NoKeyringError
proven live on GitHub Actions (P11-QUATER-LIVE, run 35706413067) has to
keep holding as the code evolves. No DB, no network, no real credentials
— keyring.get_password is monkeypatched, never actually called against a
real backend.

Run with: python3 -m pytest scripts/test_gold_credentials.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "gold"))
import _gold_db  # noqa: E402


def test_github_runner_path_uses_env_never_touches_keyring(monkeypatch):
    """The exact scenario that failed live: HH_ETL_WRITER_DATABASE_URL
    set (GitHub Secret), no OS Keychain available at all."""
    monkeypatch.setenv("HH_ETL_WRITER_DATABASE_URL", "postgresql://runner-env-url")
    monkeypatch.delenv("HH_ECOM_DB_URL_OVERRIDE", raising=False)

    def _keyring_must_not_be_called(*a, **kw):
        raise AssertionError("keyring.get_password() must not be called when HH_ETL_WRITER_DATABASE_URL is set")

    monkeypatch.setattr(_gold_db.keyring, "get_password", _keyring_must_not_be_called)

    assert _gold_db.resolve_writer_url() == "postgresql://runner-env-url"


def test_rehearsal_override_used_when_production_env_absent(monkeypatch):
    monkeypatch.delenv("HH_ETL_WRITER_DATABASE_URL", raising=False)
    monkeypatch.setenv("HH_ECOM_DB_URL_OVERRIDE", "postgresql://rehearsal-branch-url")

    def _keyring_must_not_be_called(*a, **kw):
        raise AssertionError("keyring.get_password() must not be called when HH_ECOM_DB_URL_OVERRIDE is set")

    monkeypatch.setattr(_gold_db.keyring, "get_password", _keyring_must_not_be_called)

    assert _gold_db.resolve_writer_url() == "postgresql://rehearsal-branch-url"


def test_mac_keychain_fallback_when_both_env_vars_absent(monkeypatch):
    monkeypatch.delenv("HH_ETL_WRITER_DATABASE_URL", raising=False)
    monkeypatch.delenv("HH_ECOM_DB_URL_OVERRIDE", raising=False)

    calls = []

    def _fake_keyring(service, account):
        calls.append((service, account))
        return "postgresql://mac-keychain-url"

    monkeypatch.setattr(_gold_db.keyring, "get_password", _fake_keyring)

    assert _gold_db.resolve_writer_url() == "postgresql://mac-keychain-url"
    assert calls == [("HH_ECOM_NEON", "hh_etl_writer_database_url")]


def test_hard_fails_clearly_when_every_source_is_missing(monkeypatch):
    monkeypatch.delenv("HH_ETL_WRITER_DATABASE_URL", raising=False)
    monkeypatch.delenv("HH_ECOM_DB_URL_OVERRIDE", raising=False)
    monkeypatch.setattr(_gold_db.keyring, "get_password", lambda *a, **kw: None)

    with pytest.raises(RuntimeError, match="hh_etl_writer_database_url missing"):
        _gold_db.resolve_writer_url()


def test_no_credential_value_ever_printed(monkeypatch, capsys):
    """resolve_writer_url() itself must never print anything — the only
    thing that could leak a credential to stdout/stderr is a caller
    printing the return value, which no caller in scripts/gold/ does
    (see test_only_gold_db_resolves_keyring_directly + manual review)."""
    monkeypatch.setenv("HH_ETL_WRITER_DATABASE_URL", "postgresql://secret-should-not-print")
    url = _gold_db.resolve_writer_url()
    captured = capsys.readouterr()
    assert "secret-should-not-print" not in captured.out
    assert "secret-should-not-print" not in captured.err
    assert url == "postgresql://secret-should-not-print"  # returned, not printed — the caller's job to protect
