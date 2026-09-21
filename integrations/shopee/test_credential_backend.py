"""
Credential-backend tests for the Shopee GitHub Actions portability work
(Phase 6C, P10-BIS): rotating-state precedence on read, durable persistence
on write, and the refresh flow that ties them together.

    python3 -m pytest integrations/shopee/test_credential_backend.py -v
"""
from __future__ import annotations

import importlib
import json
import time
from types import SimpleNamespace

import pytest

import keychain
import shopee_client


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Every test starts with a clean slate: no SHOPEE_* / GitHub Actions
    env vars leaking in from the real shell, and Keychain never actually
    touched (any accidental call fails loudly instead of hitting the real
    macOS Keychain or a real Linux keyring backend)."""
    for key in list(__import__("os").environ):
        if key.startswith("SHOPEE_") or key in (
            "GITHUB_ACTIONS",
            "GITHUB_REPOSITORY",
            "GH_SECRETS_WRITER_TOKEN",
        ):
            monkeypatch.delenv(key, raising=False)

    def _boom(*a, **k):
        raise AssertionError("unexpected real keyring call in a test")

    monkeypatch.setattr(keychain.keyring, "get_password", _boom)
    monkeypatch.setattr(keychain.keyring, "set_password", _boom)
    yield


# --- T13 / T14: read precedence -------------------------------------------------

def test_t13_legacy_individual_secret_fallback_still_works(monkeypatch):
    monkeypatch.setenv("SHOPEE_ACCESS_TOKEN", "legacy-access-token")
    assert keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN) == "legacy-access-token"


def test_t14_bundled_empty_field_falls_through_not_substitutes(monkeypatch):
    monkeypatch.setenv("SHOPEE_TOKEN_STATE_JSON", json.dumps({
        "access_token": "",
        "refresh_token": "bundled-refresh",
        "access_token_expire_at": None,
        "refresh_token_expire_at": "999999",
    }))
    monkeypatch.setenv("SHOPEE_ACCESS_TOKEN", "legacy-access-token")
    monkeypatch.setenv("SHOPEE_ACCESS_TOKEN_EXPIRE_AT", "legacy-expire-at")

    # access_token is "" in the bundle -> must fall through to legacy env,
    # never silently return "" and never return a different field's value.
    assert keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN) == "legacy-access-token"
    # refresh_token is present and non-empty in the bundle -> used as-is.
    assert keychain.get_secret(keychain.ACCOUNT_REFRESH_TOKEN) == "bundled-refresh"
    # access_token_expire_at is null in the bundle -> falls through.
    assert keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN_EXPIRE_AT) == "legacy-expire-at"
    # refresh_token_expire_at is present -> used as-is, not "999999" != value bug.
    assert keychain.get_secret(keychain.ACCOUNT_REFRESH_TOKEN_EXPIRE_AT) == "999999"


def test_bundled_state_takes_precedence_over_legacy(monkeypatch):
    monkeypatch.setenv("SHOPEE_TOKEN_STATE_JSON", json.dumps({"access_token": "fresh"}))
    monkeypatch.setenv("SHOPEE_ACCESS_TOKEN", "stale")
    assert keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN) == "fresh"


def test_malformed_bundled_json_falls_through(monkeypatch):
    monkeypatch.setenv("SHOPEE_TOKEN_STATE_JSON", "{not valid json")
    monkeypatch.setenv("SHOPEE_ACCESS_TOKEN", "legacy-access-token")
    assert keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN) == "legacy-access-token"


def test_static_fields_never_read_from_bundle(monkeypatch):
    # LIVE_PARTNER_KEY isn't a rotating field — bundle content must not
    # affect it even if (incorrectly) present there.
    monkeypatch.setenv("SHOPEE_TOKEN_STATE_JSON", json.dumps({"LIVE_PARTNER_KEY": "should-be-ignored"}))
    monkeypatch.setenv("SHOPEE_LIVE_PARTNER_KEY", "legacy-partner-key")
    assert keychain.get_secret(keychain.ACCOUNT_LIVE_PARTNER_KEY) == "legacy-partner-key"


# --- T7 / T8: backend selection on write -----------------------------------------

def test_t8_local_backend_still_uses_keychain(monkeypatch):
    calls = []
    monkeypatch.setattr(keychain.keyring, "set_password", lambda service, account, value: calls.append((account, value)))
    keychain.persist_rotating_state("new-access", "new-refresh", "111", "222")
    assert ("ACCESS_TOKEN", "new-access") in calls
    assert ("REFRESH_TOKEN", "new-refresh") in calls
    assert ("ACCESS_TOKEN_EXPIRE_AT", "111") in calls
    assert ("REFRESH_TOKEN_EXPIRE_AT", "222") in calls


def test_t7_github_backend_does_not_call_keychain(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_SECRETS_WRITER_TOKEN", "writer-token")

    import github_secrets_writer
    monkeypatch.setattr(github_secrets_writer, "put_secret_with_retry", lambda *a, **k: True)

    # keyring.set_password is already wired to _boom by the autouse fixture —
    # if persist_rotating_state ever called it, this test would fail via
    # that assertion, not silently pass.
    keychain.persist_rotating_state("new-access", "new-refresh", "111", "222")


# --- T4 / T5 / T6: bundled state is complete and atomic --------------------------

def test_t4_t5_t6_bundle_contains_all_four_fields_consistently(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_SECRETS_WRITER_TOKEN", "writer-token")

    import github_secrets_writer
    captured = {}

    def _capture(name, value, repo, token, **kwargs):
        captured["name"] = name
        captured["value"] = value
        captured["repo"] = repo
        captured["token"] = token
        return True

    monkeypatch.setattr(github_secrets_writer, "put_secret_with_retry", _capture)
    keychain.persist_rotating_state("new-access", "new-refresh", "111", "222")

    assert captured["name"] == "SHOPEE_TOKEN_STATE_JSON"
    payload = json.loads(captured["value"])
    assert payload == {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "access_token_expire_at": "111",
        "refresh_token_expire_at": "222",
    }
    assert captured["repo"] == "owner/repo"
    assert captured["token"] == "writer-token"


# --- T12: current process sees new state immediately (before next run's env even exists) ---

def test_t12_process_env_updated_immediately_on_github_persist(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_SECRETS_WRITER_TOKEN", "writer-token")

    import github_secrets_writer
    monkeypatch.setattr(github_secrets_writer, "put_secret_with_retry", lambda *a, **k: True)

    keychain.persist_rotating_state("new-access", "new-refresh", "111", "222")

    # Reload of the credential backend within the same process (no new env
    # injected by anything external) must already see the new state.
    assert keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN) == "new-access"
    assert keychain.get_secret(keychain.ACCOUNT_REFRESH_TOKEN) == "new-refresh"


# --- T9 / T10: durable-write retry semantics --------------------------------------

def test_t9_transient_failure_then_retry_succeeds(monkeypatch):
    import github_secrets_writer

    attempts = {"n": 0}

    def _flaky_put(name, value, repo, token):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise github_secrets_writer.GitHubSecretsError("transient")
        return None  # success

    monkeypatch.setattr(github_secrets_writer, "put_secret", _flaky_put)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(github_secrets_writer.time, "sleep", lambda *_: None)

    ok = github_secrets_writer.put_secret_with_retry("NAME", "value", "owner/repo", "token", attempts=5, base_delay=0)
    assert ok is True
    assert attempts["n"] == 3


def test_t10_permanent_failure_is_hard_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_SECRETS_WRITER_TOKEN", "writer-token")

    import github_secrets_writer
    monkeypatch.setattr(github_secrets_writer, "put_secret_with_retry", lambda *a, **k: False)

    with pytest.raises(keychain.CredentialPersistenceCriticalFailure):
        keychain.persist_rotating_state("new-access", "new-refresh", "111", "222")


def test_missing_repo_or_writer_token_is_hard_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    # GITHUB_REPOSITORY / GH_SECRETS_WRITER_TOKEN intentionally left unset.
    with pytest.raises(keychain.CredentialPersistenceCriticalFailure):
        keychain.persist_rotating_state("new-access", "new-refresh", "111", "222")


# --- T1 / T2 / T3: client-level refresh trigger and immediate use ----------------

def test_t1_valid_access_token_no_refresh(monkeypatch):
    monkeypatch.setenv("SHOPEE_ACCESS_TOKEN_EXPIRE_AT", str(int(time.time()) + 3600))
    called = {"n": 0}
    monkeypatch.setattr(shopee_client, "_do_refresh_marker", None, raising=False)

    def _fail_if_called():
        called["n"] += 1
        raise AssertionError("refresh should not have been triggered")

    # Patch the deferred import target so a call would be observable/fail loudly.
    import sys
    fake_refresh_module = SimpleNamespace(main=_fail_if_called)
    monkeypatch.setitem(sys.modules, "refresh_token", fake_refresh_module)

    shopee_client._refresh_if_needed()
    assert called["n"] == 0


def test_t2_expired_access_valid_refresh_triggers_refresh_once(monkeypatch):
    monkeypatch.setenv("SHOPEE_ACCESS_TOKEN_EXPIRE_AT", str(int(time.time()) - 10))
    called = {"n": 0}

    import sys
    def _ok_refresh():
        called["n"] += 1
        return 0

    fake_refresh_module = SimpleNamespace(main=_ok_refresh)
    monkeypatch.setitem(sys.modules, "refresh_token", fake_refresh_module)

    shopee_client._refresh_if_needed()
    assert called["n"] == 1


def test_t3_refresh_result_used_immediately_by_client(monkeypatch):
    """End-to-end within one process: an expired token triggers a refresh
    that persists via the GitHub Actions path, and the very next get_secret
    call in the same process (as ShopeeClient.get() would make) already
    sees the new access token — without needing a new run/new env."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_SECRETS_WRITER_TOKEN", "writer-token")
    monkeypatch.setenv("SHOPEE_ACCESS_TOKEN_EXPIRE_AT", str(int(time.time()) - 10))

    import github_secrets_writer
    monkeypatch.setattr(github_secrets_writer, "put_secret_with_retry", lambda *a, **k: True)

    import sys

    def _real_ish_refresh():
        keychain.persist_rotating_state("brand-new-access-token", "brand-new-refresh-token", "999999999", "999999999")
        return 0

    fake_refresh_module = SimpleNamespace(main=_real_ish_refresh)
    monkeypatch.setitem(sys.modules, "refresh_token", fake_refresh_module)

    shopee_client._refresh_if_needed()

    assert keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN) == "brand-new-access-token"


# --- T11: no token contents in logs -----------------------------------------------

def test_t11_refresh_flow_never_prints_token_values(monkeypatch, capsys):
    import refresh_token as refresh_token_module

    monkeypatch.setenv("SHOPEE_LIVE_PARTNER_KEY", "fake-partner-key")
    monkeypatch.setenv("SHOPEE_REFRESH_TOKEN", "fake-old-refresh-token")
    monkeypatch.setenv("SHOPEE_SHOP_ID", "12345")

    secret_access = "SECRET-ACCESS-VALUE-XYZ"
    secret_refresh = "SECRET-REFRESH-VALUE-XYZ"

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": secret_access, "refresh_token": secret_refresh, "expire_in": 14400}

    monkeypatch.setattr(refresh_token_module.requests, "post", lambda *a, **k: _FakeResponse())
    monkeypatch.setattr(refresh_token_module, "persist_rotating_state", lambda *a, **k: None)

    rc = refresh_token_module.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert secret_access not in out
    assert secret_refresh not in out
    assert "fake-old-refresh-token" not in out
    assert "fake-partner-key" not in out
    assert "refresh_attempted = true" in out
    assert "durable_persist_succeeded = true" in out
