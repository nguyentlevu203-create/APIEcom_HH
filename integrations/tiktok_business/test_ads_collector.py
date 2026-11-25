"""P15 — pure tests for the TikTok API for Business ads collector and the
TIKTOK/ads worker window logic. No HTTP, no DB, no Keychain (fake IDs)."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ads_collector as ac  # noqa: E402

STORE = "1000000000000000001"
ADV = "2000000000000000002"


def gm_row(cid, day, cost, net, orders, revenue):
    return {"dimensions": {"campaign_id": cid, "stat_time_day": f"{day} 00:00:00"},
            "metrics": {"cost": cost, "net_cost": net, "orders": orders, "gross_revenue": revenue}}


# ---------------- normalisation ----------------

def test_gmv_max_row_maps_cost_to_spend_and_keeps_net_cost():
    rows = ac.normalize_gmv_max([gm_row("11", "2026-11-20", "100000", "95000", "5", "400000")],
                                ADV, STORE, {"11": "Camp A"})
    assert len(rows) == 1
    r = rows[0]
    assert r["spend"] == Decimal("100000") and r["net_spend"] == Decimal("95000")
    assert r["orders"] == 5 and r["gmv"] == Decimal("400000")
    assert r["roas"] == round(Decimal("400000") / Decimal("100000"), 4)
    assert r["business_date"] == date(2026, 11, 20)
    assert r["shop_id"] == STORE and r["attribution_basis"] == "GMV_MAX" and r["campaign_name"] == "Camp A"


def test_gmv_max_idle_zero_rows_are_dropped():
    rows = ac.normalize_gmv_max([gm_row("11", "2026-11-20", "0", "0", "0", "0")], ADV, STORE, {})
    assert rows == []


def test_gmv_max_orders_without_cost_are_kept_and_roas_is_none():
    rows = ac.normalize_gmv_max([gm_row("11", "2026-11-20", "0", "0", "2", "500")], ADV, STORE, {})
    assert len(rows) == 1 and rows[0]["roas"] is None


def test_auction_rows_are_not_store_linked():
    rows = ac.normalize_auction([{
        "dimensions": {"campaign_id": "9", "stat_time_day": "2026-11-13 00:00:00"},
        "metrics": {"campaign_name": "Video", "spend": "50000", "impressions": "20000",
                    "clicks": "0", "conversion": "0"}}], ADV)
    assert rows[0]["shop_id"] is None and rows[0]["attribution_basis"] == "AUCTION"
    assert rows[0]["spend"] == Decimal("50000") and rows[0]["ctr"] == Decimal("0")


def test_auction_idle_rows_dropped():
    rows = ac.normalize_auction([{
        "dimensions": {"campaign_id": "9", "stat_time_day": "2026-11-13 00:00:00"},
        "metrics": {"spend": "0", "impressions": "0", "clicks": "0"}}], ADV)
    assert rows == []


def test_coverage_rows_one_per_day_zero_spend():
    rows = ac.coverage_rows(STORE, [date(2026, 11, 1), date(2026, 11, 2)])
    assert [r["business_date"] for r in rows] == [date(2026, 11, 1), date(2026, 11, 2)]
    assert all(r["spend"] == 0 and r["attribution_basis"] == "COVERAGE"
               and r["campaign_id"] == ac.COVERAGE_CAMPAIGN_ID for r in rows)


@pytest.mark.parametrize("raw,expected", [("12.5", Decimal("12.5")), ("-", None), ("", None), (None, None), ("x", None)])
def test_dec(raw, expected):
    assert ac.dec(raw) == expected


# ---------------- credentials ----------------

def test_credentials_from_env_bundle_and_secret_not_in_repr():
    env = {"TIKTOK_BUSINESS_AUTH_JSON": json.dumps({"app_id": "1", "secret": "s3cret", "access_token": "t0ken"})}
    c = ac.load_credentials(env)
    assert c.app_id == "1" and c.secret == "s3cret" and c.access_token == "t0ken"
    assert "s3cret" not in repr(c) and "t0ken" not in repr(c)


def test_credentials_bundle_missing_key_fails_closed():
    env = {"TIKTOK_BUSINESS_AUTH_JSON": json.dumps({"app_id": "1", "secret": "s"})}
    with pytest.raises(RuntimeError):
        ac.load_credentials(env)


# ---------------- HTTP wrapper ----------------

def test_getter_retries_transient_then_succeeds():
    calls = []

    def http_get(url, params, headers):
        calls.append(url)
        if len(calls) == 1:
            return 200, {"code": 40100, "message": "rate limit"}
        return 200, {"code": 0, "data": {"list": [1]}}

    get = ac.make_getter(ac.Credentials("1", "s", "t"), lambda m: None, sleep=lambda s: None, http_get=http_get)
    assert get("/x/", {}) == {"list": [1]} and len(calls) == 2


def test_getter_raises_on_permission_error_without_retry():
    calls = []

    def http_get(url, params, headers):
        calls.append(url)
        return 200, {"code": 40001, "message": "no permission"}

    get = ac.make_getter(ac.Credentials("1", "s", "t"), lambda m: None, sleep=lambda s: None, http_get=http_get)
    with pytest.raises(ac.TikTokBusinessError):
        get("/x/", {})
    assert len(calls) == 1


def test_paginate_follows_total_page():
    pages = {1: {"list": [1, 2], "page_info": {"total_page": 2}}, 2: {"list": [3], "page_info": {"total_page": 2}}}
    assert ac.paginate(lambda path, p: pages[p["page"]], "/x/", {}) == [1, 2, 3]


def test_collect_queries_only_advertisers_that_see_the_store_and_adds_coverage():
    def get(path, p):
        if path == "/oauth2/advertiser/get/":
            return {"list": [{"advertiser_id": "A"}, {"advertiser_id": "B"}]}
        if path == "/gmv_max/store/list/":
            return {"store_list": [{"store_id": STORE}]} if p["advertiser_id"] == "A" else {"store_list": []}
        if path == "/gmv_max/campaign/get/":
            return {"list": [{"campaign_id": "11", "campaign_name": "N"}], "page_info": {"total_page": 1}}
        if path == ac.GMV_MAX_ENDPOINT:
            assert p["advertiser_id"] == "A"
            return {"list": [gm_row("11", "2026-11-20", "100", "90", "1", "400")], "page_info": {"total_page": 1}}
        if path == ac.AUCTION_ENDPOINT:
            return {"list": [], "page_info": {"total_page": 1}}
        raise AssertionError(path)

    rows = ac.collect(get, ac.Credentials("1", "s", "t"), STORE, date(2026, 11, 20), date(2026, 11, 21), lambda m: None)
    kinds = sorted(r["attribution_basis"] for r in rows)
    assert kinds == ["COVERAGE", "COVERAGE", "GMV_MAX"]


# ---------------- worker window ----------------

def _worker():
    sys.path.insert(0, str(HERE.parent / "tiktok_shop" / "pilot_reporting"))
    import incr_worker  # noqa: E402
    return incr_worker


UTC = timezone.utc


def test_window_pulls_trailing_days_for_late_adjustments():
    w = _worker()
    now = datetime(2026, 11, 25, 5, 0, tzinfo=UTC)   # 12:00 VN
    s, e = w._tiktok_ads_days(datetime(2026, 11, 25, 4, 0, tzinfo=UTC), now, now)
    assert e == date(2026, 11, 25) and s == date(2026, 11, 23)


def test_window_caps_long_catchup():
    w = _worker()
    now = datetime(2026, 11, 25, 5, 0, tzinfo=UTC)
    s, e = w._tiktok_ads_days(datetime(2026, 8, 1, 0, 0, tzinfo=UTC), now, now)
    assert (e - s).days + 1 == w.TIKTOK_ADS_MAX_DAYS_PER_CALL


def test_window_target_date_is_exactly_one_day():
    w = _worker()
    now = datetime(2026, 11, 25, 5, 0, tzinfo=UTC)
    assert w._tiktok_ads_days(now, now, now, target_date=date(2026, 11, 20)) == (date(2026, 11, 20), date(2026, 11, 20))
