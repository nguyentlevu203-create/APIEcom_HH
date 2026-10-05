"""
P11 RECOVERY A — unit tests for durable TikTok token persistence
(integrations/tiktok_shop/token_persistence.py + refresh_token.do_refresh
wiring + TOKEN_STATE_PERSISTENCE_FAILED propagation into the cycle
verdict). Pure: fake tokens only, no HTTP, no GitHub, no Keychain.

Run with: python3 -m pytest integrations/tiktok_shop/pilot_reporting/test_token_persistence.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TIKTOK_DIR = Path(__file__).resolve().parent.parent
ROOT = TIKTOK_DIR.parent.parent
sys.path.insert(0, str(TIKTOK_DIR))
import refresh_token as refresh_mod  # noqa: E402
import token_persistence  # noqa: E402
import token_store  # noqa: E402

FAKE_ACCESS = "FAKE-ACCESS-aaaaaaaaaaaaaaaa"
FAKE_REFRESH = "FAKE-REFRESH-bbbbbbbbbbbbbbbb"
FAKE_NEW_ACCESS = "FAKE-NEW-ACCESS-cccccccccccc"
FAKE_NEW_REFRESH = "FAKE-NEW-REFRESH-dddddddddddd"
FAKE_WRITER = "FAKE-GH-WRITER-eeeeeeeeeeee"
ALL_FAKES = (FAKE_ACCESS, FAKE_REFRESH, FAKE_NEW_ACCESS, FAKE_NEW_REFRESH, FAKE_WRITER)


class FakeWriter:
    def __init__(self, ok=True, raises=None):
        self.ok, self.raises, self.calls = ok, raises, []

    def put_secret_with_retry(self, name, value, repo, token):
        self.calls.append((name, value, repo, token))
        if self.raises:
            raise self.raises
        return self.ok


class FakeClient:
    def __init__(self, app_key, app_secret):
        pass

    def refresh_token(self, refresh_token_value):
        assert refresh_token_value == FAKE_REFRESH
        return {
            "access_token": FAKE_NEW_ACCESS,
            "refresh_token": FAKE_NEW_REFRESH,
            "access_token_expire_in": 1900000000,
            "refresh_token_expire_in": 4900000000,
            "granted_scopes": ["seller.order.info"],
        }


@pytest.fixture
def tokens_file(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({
        "access_token": FAKE_ACCESS, "refresh_token": FAKE_REFRESH,
        "access_token_expire_in": 1, "refresh_token_expire_in": 4900000000,
        "seller_name": "Fake Seller", "app_key": "fakeappkey",
    }))
    monkeypatch.setattr(token_store, "TOKENS_PATH", path)
    monkeypatch.setattr(refresh_mod, "TikTokShopClient", FakeClient)
    return path


@pytest.fixture
def github_env(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "fake-owner/fake-repo")
    monkeypatch.setenv("GH_SECRETS_WRITER_TOKEN", FAKE_WRITER)


def _use_writer(monkeypatch, writer):
    monkeypatch.setattr(token_persistence, "_load_writer", lambda: writer)


def _assert_no_secret(text):
    for fake in ALL_FAKES:
        assert fake not in text


def test_no_refresh_needed_writes_no_secret(tokens_file, github_env, monkeypatch):
    writer = FakeWriter()
    _use_writer(monkeypatch, writer)
    fresh = json.loads(tokens_file.read_text())
    fresh["access_token_expire_in"] = 4800000000
    tokens_file.write_text(json.dumps(fresh))
    result = refresh_mod.ensure_fresh_token("fakesecret")
    assert result["access_token"] == FAKE_ACCESS
    assert writer.calls == []


def test_local_mac_refresh_writes_file_only(tokens_file, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    writer = FakeWriter()
    _use_writer(monkeypatch, writer)
    merged = refresh_mod.do_refresh("fakeappkey", "fakesecret", FAKE_REFRESH)
    assert merged["access_token"] == FAKE_NEW_ACCESS
    assert json.loads(tokens_file.read_text())["refresh_token"] == FAKE_NEW_REFRESH
    assert writer.calls == []
    out = capsys.readouterr()
    _assert_no_secret(out.out + out.err)


def test_github_refresh_persists_full_bundle_once(tokens_file, github_env, monkeypatch, capsys):
    writer = FakeWriter(ok=True)
    _use_writer(monkeypatch, writer)
    refresh_mod.do_refresh("fakeappkey", "fakesecret", FAKE_REFRESH)
    assert len(writer.calls) == 1, "exactly one atomic secret write, never per-field secrets"
    name, value, repo, token = writer.calls[0]
    assert name == "TIKTOK_TOKENS_JSON"
    assert repo == "fake-owner/fake-repo" and token == FAKE_WRITER
    payload = json.loads(value)
    # Same full bundle the workflow provisions back into tokens.json next run.
    assert payload == json.loads(tokens_file.read_text())
    assert payload["access_token"] == FAKE_NEW_ACCESS
    assert payload["refresh_token"] == FAKE_NEW_REFRESH
    assert payload["seller_name"] == "Fake Seller"  # merged, not replaced
    assert payload["app_key"] == "fakeappkey"
    assert token_persistence.validate_bundle(payload) == []
    out = capsys.readouterr()
    _assert_no_secret(out.out + out.err)


@pytest.mark.parametrize("writer", [FakeWriter(ok=False), FakeWriter(raises=ConnectionError(FAKE_NEW_REFRESH))])
def test_github_refresh_persist_failure_fails_closed(tokens_file, github_env, monkeypatch, capsys, writer):
    _use_writer(monkeypatch, writer)
    with pytest.raises(token_persistence.TokenStatePersistenceFailed) as exc:
        refresh_mod.do_refresh("fakeappkey", "fakesecret", FAKE_REFRESH)
    msg = str(exc.value)
    assert msg.startswith("TOKEN_STATE_PERSISTENCE_FAILED:")
    _assert_no_secret(msg)
    out = capsys.readouterr()
    _assert_no_secret(out.out + out.err)


def test_github_missing_writer_credential_fails_closed(tokens_file, github_env, monkeypatch):
    monkeypatch.delenv("GH_SECRETS_WRITER_TOKEN")
    writer = FakeWriter()
    _use_writer(monkeypatch, writer)
    with pytest.raises(token_persistence.TokenStatePersistenceFailed, match="^TOKEN_STATE_PERSISTENCE_FAILED"):
        refresh_mod.do_refresh("fakeappkey", "fakesecret", FAKE_REFRESH)
    assert writer.calls == []


def test_schema_check_rejects_incomplete_bundle(github_env, monkeypatch):
    writer = FakeWriter()
    _use_writer(monkeypatch, writer)
    with pytest.raises(token_persistence.TokenStatePersistenceFailed, match="missing:refresh_token"):
        token_persistence.persist_refreshed_bundle(
            {"access_token": FAKE_ACCESS, "access_token_expire_in": 1, "refresh_token_expire_in": 2})
    assert writer.calls == []


def test_real_writer_module_loads_by_path_without_shadowing_config():
    writer = token_persistence._load_writer()
    assert callable(writer.put_secret_with_retry)
    import config  # TikTok's config must still be the one on sys.path
    assert Path(config.__file__).resolve().parent == TIKTOK_DIR


def test_failure_code_promoted_into_cycle_verdict():
    sys.path.insert(0, str(ROOT / "pipelines"))
    sys.path.insert(0, str(ROOT / "scripts"))
    import incr_common as ic
    import run_production_cycle as cycle

    assert ic.TOKEN_STATE_PERSISTENCE_FAILED == token_persistence.FAILURE_CODE
    failed = {"status": "FAIL", "error": "TOKEN_STATE_PERSISTENCE_FAILED: write to TIKTOK_TOKENS_JSON failed after retries"}
    assert ic.classify_domain_error(failed) == "TOKEN_STATE_PERSISTENCE_FAILED"

    ingestion = {
        "status": "FAILED",
        "ingestion_domain_results": {"TIKTOK/finance": {"status": "FAIL", "error_class": ic.classify_domain_error(failed)}},
        "ingestion_failed_domains": ["TIKTOK/finance"],
    }
    recon = {"status": "SUCCESS", "reconciliation_failed_domains": [], "reconciliation_domain_timings": []}
    verdict, reasons = cycle.compute_cycle_verdict(ingestion, [], recon, "GREEN")
    assert verdict == "RED"
    assert "TOKEN_STATE_PERSISTENCE_FAILED" in reasons

    ok_ingestion = {"status": "SUCCESS", "ingestion_domain_results": {}, "ingestion_failed_domains": []}
    recon_fail = {"status": "FAILED", "reconciliation_failed_domains": ["TIKTOK/orders:D1"],
                  "reconciliation_domain_timings": [{"error_class": "TOKEN_STATE_PERSISTENCE_FAILED"}]}
    verdict, reasons = cycle.compute_cycle_verdict(ok_ingestion, [], recon_fail, "GREEN")
    assert verdict == "RED" and "TOKEN_STATE_PERSISTENCE_FAILED" in reasons
