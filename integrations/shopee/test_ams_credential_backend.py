"""
Credential-backend tests for the Shopee AMS (affiliate) GitHub Actions
portability fix (Phase 6C, P11-BIS): rotating-state precedence on read,
durable persistence on write, and the refresh flow that ties them
together — mirrors test_credential_backend.py for the main Shopee app,
but for the separate AMS app (AMS_PARTNER_ID 2044772) and its own
SHOPEE_AMS_TOKEN_STATE_JSON bundle.

    python3 -m pytest integrations/shopee/test_ams_credential_backend.py -v
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import keychain
import token_exchange_ams as ams


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("SHOPEE_") or key in (
            "GITHUB_ACTIONS",
            "GITHUB_REPOSITORY",
            "GH_SECRETS_WRITER_TOKEN",
        ):
            monkeypatch.delenv(key, raising=False)

    def _boom(*a, **k):
        raise AssertionError("unexpected real keyring call in a test")

    monkeypatch.setattr(ams.keyring, "get_password", _boom)
    monkeypatch.setattr(ams.keyring, "set_password", _boom)
    yield


# --- read precedence -------------------------------------------------------

def test_legacy_individual_secret_fallback_still_works(monkeypatch):
    monkeypatch.setenv("SHOPEE_AMS_ACCESS_TOKEN", "legacy-ams-access")
    assert ams.get_secret(ams.ACCOUNT_AMS_ACCESS_TOKEN) == "legacy-ams-access"


def test_bundled_empty_field_falls_through_not_substitutes(monkeypatch):
    monkeypatch.setenv("SHOPEE_AMS_TOKEN_STATE_JSON", json.dumps({
        "access_token": "",
        "refresh_token": "bundled-ams-refresh",
        "access_token_expire_at": None,
        "refresh_token_expire_at": "999999",
    }))
    monkeypatch.setenv("SHOPEE_AMS_ACCESS_TOKEN", "legacy-ams-access")
    monkeypatch.setenv("SHOPEE_AMS_ACCESS_TOKEN_EXPIRE_AT", "legacy-ams-expire-at")

    assert ams.get_secret(ams.ACCOUNT_AMS_ACCESS_TOKEN) == "legacy-ams-access"
    assert ams.get_secret(ams.ACCOUNT_AMS_REFRESH_TOKEN) == "bundled-ams-refresh"
    assert ams.get_secret(ams.ACCOUNT_AMS_ACCESS_TOKEN_EXPIRE_AT) == "legacy-ams-expire-at"
    assert ams.get_secret(ams.ACCOUNT_AMS_REFRESH_TOKEN_EXPIRE_AT) == "999999"


def test_bundled_state_takes_precedence_over_legacy(monkeypatch):
    monkeypatch.setenv("SHOPEE_AMS_TOKEN_STATE_JSON", json.dumps({"access_token": "fresh-ams"}))
    monkeypatch.setenv("SHOPEE_AMS_ACCESS_TOKEN", "stale-ams")
    assert ams.get_secret(ams.ACCOUNT_AMS_ACCESS_TOKEN) == "fresh-ams"


def test_malformed_bundled_json_falls_through(monkeypatch):
    monkeypatch.setenv("SHOPEE_AMS_TOKEN_STATE_JSON", "{not valid json")
    monkeypatch.setenv("SHOPEE_AMS_ACCESS_TOKEN", "legacy-ams-access")
    assert ams.get_secret(ams.ACCOUNT_AMS_ACCESS_TOKEN) == "legacy-ams-access"


def test_static_fields_never_read_from_bundle(monkeypatch):
    monkeypatch.setenv("SHOPEE_AMS_TOKEN_STATE_JSON", json.dumps({"AMS_LIVE_PARTNER_KEY": "should-be-ignored"}))
    monkeypatch.setenv("SHOPEE_AMS_LIVE_PARTNER_KEY", "legacy-ams-partner-key")
    assert ams.get_secret(ams.ACCOUNT_AMS_LIVE_PARTNER_KEY) == "legacy-ams-partner-key"


def test_main_app_bundle_never_used_for_ams_fields(monkeypatch):
    """SHOPEE_TOKEN_STATE_JSON (main app) must never leak into AMS reads —
    these are two different Shopee apps with independent credentials."""
    monkeypatch.setenv("SHOPEE_TOKEN_STATE_JSON", json.dumps({"access_token": "main-app-token"}))
    monkeypatch.setenv("SHOPEE_AMS_ACCESS_TOKEN", "legacy-ams-access")
    assert ams.get_secret(ams.ACCOUNT_AMS_ACCESS_TOKEN) == "legacy-ams-access"
    # And the reverse: keychain.py's main-app get_secret must not see AMS state.
    monkeypatch.setenv("SHOPEE_AMS_TOKEN_STATE_JSON", json.dumps({"access_token": "ams-token"}))
    monkeypatch.setenv("SHOPEE_ACCESS_TOKEN", "legacy-main-access")
    assert keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN) == "main-app-token"


# --- backend selection on write --------------------------------------------

def test_local_backend_still_uses_keychain(monkeypatch):
    calls = []
    monkeypatch.setattr(ams.keyring, "set_password", lambda service, account, value: calls.append((account, value)))
    ams.persist_ams_rotating_state("new-ams-access", "new-ams-refresh", "111", "222")
    assert ("AMS_ACCESS_TOKEN", "new-ams-access") in calls
    assert ("AMS_REFRESH_TOKEN", "new-ams-refresh") in calls
    assert ("AMS_ACCESS_TOKEN_EXPIRE_AT", "111") in calls
    assert ("AMS_REFRESH_TOKEN_EXPIRE_AT", "222") in calls


def test_github_backend_does_not_call_keychain(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_SECRETS_WRITER_TOKEN", "writer-token")

    import github_secrets_writer
    monkeypatch.setattr(github_secrets_writer, "put_secret_with_retry", lambda *a, **k: True)

    ams.persist_ams_rotating_state("new-ams-access", "new-ams-refresh", "111", "222")


def test_bundle_contains_all_four_fields_consistently_and_separate_secret_name(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_SECRETS_WRITER_TOKEN", "writer-token")

    import github_secrets_writer
    captured = {}

    def _capture(name, value, repo, token, **kwargs):
        captured["name"] = name
        captured["value"] = value
        return True

    monkeypatch.setattr(github_secrets_writer, "put_secret_with_retry", _capture)
    ams.persist_ams_rotating_state("new-ams-access", "new-ams-refresh", "111", "222")

    assert captured["name"] == "SHOPEE_AMS_TOKEN_STATE_JSON"
    payload = json.loads(captured["value"])
    assert payload == {
        "access_token": "new-ams-access",
        "refresh_token": "new-ams-refresh",
        "access_token_expire_at": "111",
        "refresh_token_expire_at": "222",
    }


def test_process_env_updated_immediately_on_github_persist(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_SECRETS_WRITER_TOKEN", "writer-token")

    import github_secrets_writer
    monkeypatch.setattr(github_secrets_writer, "put_secret_with_retry", lambda *a, **k: True)

    ams.persist_ams_rotating_state("new-ams-access", "new-ams-refresh", "111", "222")
    assert ams.get_secret(ams.ACCOUNT_AMS_ACCESS_TOKEN) == "new-ams-access"
    assert ams.get_secret(ams.ACCOUNT_AMS_REFRESH_TOKEN) == "new-ams-refresh"


# --- retry / hard-failure semantics -----------------------------------------

def test_permanent_persist_failure_is_hard_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_SECRETS_WRITER_TOKEN", "writer-token")

    import github_secrets_writer
    monkeypatch.setattr(github_secrets_writer, "put_secret_with_retry", lambda *a, **k: False)

    with pytest.raises(ams.CredentialPersistenceCriticalFailure):
        ams.persist_ams_rotating_state("new-ams-access", "new-ams-refresh", "111", "222")


def test_missing_repo_or_writer_token_is_hard_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(ams.CredentialPersistenceCriticalFailure):
        ams.persist_ams_rotating_state("new-ams-access", "new-ams-refresh", "111", "222")


# --- refresh flow: no-refresh / refresh-triggered / immediate-use ----------

def test_valid_access_token_no_refresh(monkeypatch):
    """Mirrors incr_worker.run_affiliate_ams()'s own pre-flight check
    logic directly (that function is deeply embedded in a worker with
    DB/network dependencies, so the expiry-gate logic itself is what's
    under test here, exactly as it's written at incr_worker.py:512-518)."""
    monkeypatch.setenv("SHOPEE_AMS_ACCESS_TOKEN_EXPIRE_AT", str(int(time.time()) + 3600))
    expire_at = ams.get_secret(ams.ACCOUNT_AMS_ACCESS_TOKEN_EXPIRE_AT)
    needs_refresh = True
    if expire_at:
        try:
            needs_refresh = int(expire_at) - time.time() <= 600
        except ValueError:
            needs_refresh = True
    assert needs_refresh is False


def test_expired_access_token_triggers_refresh(monkeypatch):
    monkeypatch.setenv("SHOPEE_AMS_ACCESS_TOKEN_EXPIRE_AT", str(int(time.time()) - 10))
    expire_at = ams.get_secret(ams.ACCOUNT_AMS_ACCESS_TOKEN_EXPIRE_AT)
    needs_refresh = True
    if expire_at:
        try:
            needs_refresh = int(expire_at) - time.time() <= 600
        except ValueError:
            needs_refresh = True
    assert needs_refresh is True


def test_refresh_result_used_immediately(monkeypatch):
    """End-to-end within one process: a refresh that persists via the
    GitHub Actions path is immediately visible to the next get_secret()
    call, exactly as ams_get() would need for the current request."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_SECRETS_WRITER_TOKEN", "writer-token")

    import github_secrets_writer
    monkeypatch.setattr(github_secrets_writer, "put_secret_with_retry", lambda *a, **k: True)

    ams.persist_ams_rotating_state("brand-new-ams-access", "brand-new-ams-refresh", "999999999", "999999999")
    assert ams.get_secret(ams.ACCOUNT_AMS_ACCESS_TOKEN) == "brand-new-ams-access"


# --- no token contents in logs ----------------------------------------------

def test_refresh_flow_never_prints_token_values(monkeypatch, capsys):
    import refresh_token_ams as refresh_ams_module

    monkeypatch.setenv("SHOPEE_AMS_LIVE_PARTNER_KEY", "fake-ams-partner-key")
    monkeypatch.setenv("SHOPEE_AMS_REFRESH_TOKEN", "fake-old-ams-refresh-token")
    monkeypatch.setenv("SHOPEE_AMS_SHOP_ID", "12345")

    secret_access = "SECRET-AMS-ACCESS-VALUE-XYZ"
    secret_refresh = "SECRET-AMS-REFRESH-VALUE-XYZ"

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": secret_access, "refresh_token": secret_refresh, "expire_in": 14400}

    monkeypatch.setattr(refresh_ams_module.requests, "post", lambda *a, **k: _FakeResponse())
    monkeypatch.setattr(refresh_ams_module, "persist_ams_rotating_state", lambda *a, **k: None)

    rc = refresh_ams_module.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert secret_access not in out
    assert secret_refresh not in out
    assert "fake-old-ams-refresh-token" not in out
    assert "fake-ams-partner-key" not in out
    assert "ams_refresh_attempted = true" in out
    assert "ams_durable_persist_succeeded = true" in out
