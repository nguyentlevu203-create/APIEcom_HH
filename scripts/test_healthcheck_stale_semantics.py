"""
P11-HEAL — unit tests for production_healthcheck.py's STALE
classification (_classify_source_status, REQUIRED_SOURCES,
OPTIONAL_SOURCES).

Root cause fixed: mart.v_ai_source_coverage (sql/049) can report a
coverage_status of STALE for any of its 14 tracked domains, but
production_healthcheck.py's status handling only knew about CURRENT and
five other named statuses — STALE fell into the "anything else
unexpected" catch-all and was always RED, for every source regardless
of business criticality. Proven live 2026-09-23:
SOURCE[TIKTOK.live]=STALE (an engagement-analytics domain, not a P&L
input) turned the whole cycle healthcheck RED.

Run with: python3 -m pytest scripts/test_healthcheck_stale_semantics.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import production_healthcheck as hc  # noqa: E402

# The exact 14 (source_system, source_endpoint) pairs sql/049's
# `domains` CTE tracks — kept here as a literal so a future edit to
# either file fails a test loudly instead of silently drifting.
ALL_TRACKED_SOURCES = {
    ("SHOPEE", "orders"), ("SHOPEE", "returns"), ("SHOPEE", "finance"), ("SHOPEE", "ads"),
    ("SHOPEE", "product_inventory"), ("SHOPEE", "affiliate_ams"),
    ("TIKTOK", "orders"), ("TIKTOK", "returns"), ("TIKTOK", "finance"), ("TIKTOK", "affiliate"),
    ("TIKTOK", "product_analytics"), ("TIKTOK", "live"), ("TIKTOK", "product_inventory"),
    ("TIKTOK", "shop_traffic"),
}


def test_every_tracked_source_is_classified_exactly_once():
    mapped = hc.REQUIRED_SOURCES | hc.OPTIONAL_SOURCES
    assert hc.REQUIRED_SOURCES.isdisjoint(hc.OPTIONAL_SOURCES), "a source cannot be both required and optional"
    missing = ALL_TRACKED_SOURCES - mapped
    assert not missing, f"source(s) tracked by sql/049 but not classified required/optional: {missing}"
    extra = mapped - ALL_TRACKED_SOURCES
    assert not extra, f"classified source(s) sql/049 does not actually track: {extra}"


def test_current_is_always_green():
    assert hc._classify_source_status("SHOPEE", "orders", "CURRENT") == "GREEN"
    assert hc._classify_source_status("TIKTOK", "live", "CURRENT") == "GREEN"


def test_required_source_stale_is_red():
    assert hc._classify_source_status("SHOPEE", "affiliate_ams", "STALE") == "RED"
    assert hc._classify_source_status("TIKTOK", "orders", "STALE") == "RED"


def test_optional_source_stale_is_yellow():
    # The exact live regression: TIKTOK/live going STALE must not be RED.
    assert hc._classify_source_status("TIKTOK", "live", "STALE") == "YELLOW"
    assert hc._classify_source_status("TIKTOK", "product_analytics", "STALE") == "YELLOW"
    assert hc._classify_source_status("SHOPEE", "product_inventory", "STALE") == "YELLOW"


def test_known_yellow_statuses_unchanged_for_both_required_and_optional():
    for status in hc.KNOWN_YELLOW_STATUSES:
        assert hc._classify_source_status("SHOPEE", "affiliate_ams", status) == "YELLOW"
        assert hc._classify_source_status("TIKTOK", "live", status) == "YELLOW"


def test_unrecognized_status_still_fails_closed_to_red():
    assert hc._classify_source_status("SHOPEE", "orders", "SOMETHING_NOBODY_WROTE_YET") == "RED"
    assert hc._classify_source_status("TIKTOK", "live", "SOMETHING_NOBODY_WROTE_YET") == "RED"


def test_requiredness_map_is_static_not_row_count_based():
    # Sanity: the sets are plain literals, not derived from any live
    # query or count — importing the module must not touch the DB.
    assert isinstance(hc.REQUIRED_SOURCES, set)
    assert isinstance(hc.OPTIONAL_SOURCES, set)
    assert len(hc.REQUIRED_SOURCES) == 9
    assert len(hc.OPTIONAL_SOURCES) == 5
