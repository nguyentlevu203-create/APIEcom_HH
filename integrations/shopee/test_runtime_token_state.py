"""
P11 RECOVERY D — runner-local rotating token state (runtime_token_state.py)
for the main Shopee app (keychain.py) and the AMS app
(auth/token_exchange_ams.py). Fake tokens only; GitHub writer and Keychain
are always faked. Includes real child-process tests: worker A refreshes,
worker B starts from the stale job-start env and must still see A's state.

Run with: python3 -m pytest integrations/shopee/test_runtime_token_state.py -v
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import keychain
import runtime_token_state as rts
import shopee_client
import token_exchange_ams as ams

SHOPEE_DIR = Path(__file__).resolve().parent
OLD = {"access_token": "FAKE-OLD-ACCESS-1111", "refresh_token": "FAKE-OLD-REFRESH-2222",
       "access_token_expire_at": "1000", "refresh_token_expire_at": "4900000000"}
NEW_ACCESS, NEW_REFRESH = "FAKE-NEW-ACCESS-3333", "FAKE-NEW-REFRESH-4444"
FAKES = (OLD["access_token"], OLD["refresh_token"], NEW_ACCESS, NEW_REFRESH, "FAKE-WRITER-5555")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SHOPEE_") or key in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GH_SECRETS_WRITER_TOKEN",
                                                 rts.STATE_DIR_ENV):
            monkeypatch.delenv(key, raising=False)

    def _boom(*a, **k):
        raise AssertionError("unexpected real keyring call in a test")

    monkeypatch.setattr(keychain.keyring, "get_password", _boom)
    monkeypatch.setattr(keychain.keyring, "set_password", _boom)
    monkeypatch.setattr(ams.keyring, "get_password", _boom)
    monkeypatch.setattr(ams.keyring, "set_password", _boom)
    yield


@pytest.fixture
def github(monkeypatch, tmp_path):
    state_dir = tmp_path / "hh-token-state"
    state_dir.mkdir(mode=0o700)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "fake-owner/fake-repo")
    monkeypatch.setenv("GH_SECRETS_WRITER_TOKEN", "FAKE-WRITER-5555")
    monkeypatch.setenv(rts.STATE_DIR_ENV, str(state_dir))
    monkeypatch.setenv("SHOPEE_TOKEN_STATE_JSON", json.dumps(OLD))
    monkeypatch.setenv("SHOPEE_AMS_TOKEN_STATE_JSON", json.dumps(OLD))
    return state_dir


@pytest.fixture
def writer(monkeypatch):
    import github_secrets_writer
    calls = []

    def _put(name, value, repo, token, **kw):
        calls.append((name, json.loads(value)))
        return writer.ok

    writer.ok = True
    writer.calls = calls
    monkeypatch.setattr(github_secrets_writer, "put_secret_with_retry", _put)
    return writer


def _future():
    return str(int(time.time()) + 4 * 3600)


def _no_fakes(text):
    for fake in FAKES:
        assert fake not in text


# --- main app ------------------------------------------------------------------

def test_refresh_writes_runner_local_state_0600_then_secret(github, writer, monkeypatch):
    keychain.persist_rotating_state(NEW_ACCESS, NEW_REFRESH, _future(), "4900000001")
    path = github / "SHOPEE_TOKEN_STATE_JSON.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    local = json.loads(path.read_text())
    assert local["access_token"] == NEW_ACCESS and local["refresh_token"] == NEW_REFRESH
    assert writer.calls == [("SHOPEE_TOKEN_STATE_JSON", local)]  # full bundle, one atomic secret write
    assert not list(github.glob(".*"))  # no leftover temp file


def test_second_worker_sees_refreshed_state_and_does_not_refresh(github, writer, monkeypatch):
    keychain.persist_rotating_state(NEW_ACCESS, NEW_REFRESH, _future(), "4900000001")
    # A new worker subprocess inherits the job-start env, not worker A's os.environ.
    monkeypatch.setenv("SHOPEE_TOKEN_STATE_JSON", json.dumps(OLD))
    assert keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN) == NEW_ACCESS
    assert keychain.get_secret(keychain.ACCOUNT_REFRESH_TOKEN) == NEW_REFRESH

    refreshes = []
    fake_refresh = type(sys)("refresh_token")
    fake_refresh.main = lambda: refreshes.append(1) or 0
    monkeypatch.setitem(sys.modules, "refresh_token", fake_refresh)
    shopee_client._refresh_if_needed()
    assert refreshes == []


def test_without_local_state_stale_env_bundle_still_triggers_refresh(github, monkeypatch):
    refreshes = []
    fake_refresh = type(sys)("refresh_token")
    fake_refresh.main = lambda: refreshes.append(1) or 0
    monkeypatch.setitem(sys.modules, "refresh_token", fake_refresh)
    shopee_client._refresh_if_needed()
    assert refreshes == [1]  # the pre-fix behavior, now only for the first worker


def test_missing_local_file_falls_back_to_env_bundle(github):
    assert not (github / "SHOPEE_TOKEN_STATE_JSON.json").exists()
    assert keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN) == OLD["access_token"]


@pytest.mark.parametrize("content", ["not json", "[1, 2]", json.dumps({**OLD, "refresh_token": ""})])
def test_present_malformed_local_file_fails_closed(github, content):
    path = github / "SHOPEE_TOKEN_STATE_JSON.json"
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(rts.RuntimeTokenStateError) as exc:
        keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN)
    _no_fakes(str(exc.value))


def test_present_local_file_with_unsafe_permissions_fails_closed(github):
    path = github / "SHOPEE_TOKEN_STATE_JSON.json"
    path.write_text(json.dumps({**OLD, "access_token": NEW_ACCESS}))
    path.chmod(0o644)
    with pytest.raises(rts.RuntimeTokenStateError, match="permissions"):
        keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN)


def test_present_local_symlink_fails_closed(github, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(OLD))
    (github / "SHOPEE_TOKEN_STATE_JSON.json").symlink_to(target)
    with pytest.raises(rts.RuntimeTokenStateError, match="regular file"):
        keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN)


def test_github_secret_failure_is_still_hard_failure(github, writer):
    writer.ok = False
    with pytest.raises(keychain.CredentialPersistenceCriticalFailure) as exc:
        keychain.persist_rotating_state(NEW_ACCESS, NEW_REFRESH, _future(), "4900000001")
    _no_fakes(str(exc.value))
    assert len(writer.calls) == 1


def test_local_state_write_failure_still_attempts_github_secret_then_fails(github, writer, monkeypatch):
    github.chmod(0o500)  # directory not writable
    try:
        with pytest.raises(keychain.CredentialPersistenceCriticalFailure) as exc:
            keychain.persist_rotating_state(NEW_ACCESS, NEW_REFRESH, _future(), "4900000001")
    finally:
        github.chmod(0o700)
    _no_fakes(str(exc.value))
    assert "succeeded" in str(exc.value)
    assert writer.calls == [("SHOPEE_TOKEN_STATE_JSON", {
        "access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH,
        "access_token_expire_at": writer.calls[0][1]["access_token_expire_at"], "refresh_token_expire_at": "4900000001"})]


def test_secret_attempted_before_local_file(github, writer, monkeypatch):
    order = []
    import github_secrets_writer
    monkeypatch.setattr(github_secrets_writer, "put_secret_with_retry", lambda *a, **k: order.append("secret") or True)
    real_write = keychain.write_runtime_state
    monkeypatch.setattr(keychain, "write_runtime_state", lambda *a: order.append("local") or real_write(*a))
    keychain.persist_rotating_state(NEW_ACCESS, NEW_REFRESH, _future(), "4900000001")
    assert order == ["secret", "local"]


def test_state_dir_inside_repo_is_refused(github, writer, monkeypatch):
    monkeypatch.setenv(rts.STATE_DIR_ENV, str(SHOPEE_DIR / "should-not-exist"))
    with pytest.raises(rts.RuntimeTokenStateError, match="outside the repository"):
        rts.read_state("SHOPEE_TOKEN_STATE_JSON")
    with pytest.raises(keychain.CredentialPersistenceCriticalFailure, match="outside the repository"):
        keychain.persist_rotating_state(NEW_ACCESS, NEW_REFRESH, _future(), "4900000001")
    assert len(writer.calls) == 1  # durable secret still attempted
    assert not (SHOPEE_DIR / "should-not-exist").exists()


def test_local_mac_never_touches_runtime_state(monkeypatch, tmp_path):
    monkeypatch.setenv(rts.STATE_DIR_ENV, str(tmp_path))  # even if set, ignored without GITHUB_ACTIONS
    stored = {}
    monkeypatch.setattr(keychain.keyring, "set_password", lambda svc, acct, val: stored.__setitem__(acct, val))
    keychain.persist_rotating_state(NEW_ACCESS, NEW_REFRESH, "1", "2")
    assert stored[keychain.ACCOUNT_ACCESS_TOKEN] == NEW_ACCESS
    assert list(tmp_path.iterdir()) == []
    assert rts.write_state("SHOPEE_TOKEN_STATE_JSON", "{}") is False


# --- AMS -----------------------------------------------------------------------

def test_ams_refresh_shared_with_later_workers(github, writer, monkeypatch):
    ams.persist_ams_rotating_state(NEW_ACCESS, NEW_REFRESH, _future(), "4900000001")
    assert (github / "SHOPEE_AMS_TOKEN_STATE_JSON.json").exists()
    assert not (github / "SHOPEE_TOKEN_STATE_JSON.json").exists()  # never mixed with the main app
    monkeypatch.setenv("SHOPEE_AMS_TOKEN_STATE_JSON", json.dumps(OLD))
    assert ams.get_secret(ams.ACCOUNT_AMS_ACCESS_TOKEN) == NEW_ACCESS
    assert keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN) == OLD["access_token"]
    assert writer.calls[0][0] == "SHOPEE_AMS_TOKEN_STATE_JSON"


def test_ams_github_secret_failure_is_hard_failure(github, writer):
    writer.ok = False
    with pytest.raises(keychain.CredentialPersistenceCriticalFailure):
        ams.persist_ams_rotating_state(NEW_ACCESS, NEW_REFRESH, _future(), "4900000001")


def test_ams_local_write_failure_still_attempts_github_secret(github, writer):
    github.chmod(0o500)
    try:
        with pytest.raises(keychain.CredentialPersistenceCriticalFailure):
            ams.persist_ams_rotating_state(NEW_ACCESS, NEW_REFRESH, _future(), "4900000001")
    finally:
        github.chmod(0o700)
    assert [c[0] for c in writer.calls] == ["SHOPEE_AMS_TOKEN_STATE_JSON"]


def test_ams_present_malformed_local_file_fails_closed(github):
    path = github / "SHOPEE_AMS_TOKEN_STATE_JSON.json"
    path.write_text("{}")
    path.chmod(0o600)
    with pytest.raises(rts.RuntimeTokenStateError):
        ams.get_secret(ams.ACCOUNT_AMS_ACCESS_TOKEN)


# --- real child processes ------------------------------------------------------

_CHILD = r"""
import json, os, sys
sys.path[:0] = [{shopee!r}, {auth!r}]
import keychain, github_secrets_writer
github_secrets_writer.put_secret_with_retry = lambda *a, **k: True
import keyring
keyring.get_password = keyring.set_password = lambda *a, **k: (_ for _ in ()).throw(AssertionError("keyring"))
role = sys.argv[1]
if role == "A":
    keychain.persist_rotating_state({new_access!r}, {new_refresh!r}, {future!r}, "4900000001")
    print("A_DONE")
else:
    print("B_SEES_NEW=" + str(keychain.get_secret(keychain.ACCOUNT_ACCESS_TOKEN) == {new_access!r}))
"""


def test_real_subprocesses_worker_b_uses_worker_a_refresh(github, tmp_path):
    script = tmp_path / "child.py"
    script.write_text(_CHILD.format(shopee=str(SHOPEE_DIR), auth=str(SHOPEE_DIR / "auth"),
                                    new_access=NEW_ACCESS, new_refresh=NEW_REFRESH, future=_future()))
    env = dict(os.environ)  # job-start env: stale SHOPEE_TOKEN_STATE_JSON for both children
    a = subprocess.run([sys.executable, str(script), "A"], env=env, capture_output=True, text=True, timeout=60)
    b = subprocess.run([sys.executable, str(script), "B"], env=env, capture_output=True, text=True, timeout=60)
    assert a.returncode == 0, a.stderr[-500:]
    assert "B_SEES_NEW=True" in b.stdout, b.stderr[-500:]
    _no_fakes(a.stdout + a.stderr + b.stdout + b.stderr)
