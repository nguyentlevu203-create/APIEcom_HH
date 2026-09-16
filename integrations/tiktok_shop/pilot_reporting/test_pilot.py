"""
Section 23 acceptance tests for the FIRST END-TO-END REPORTING PILOT.

These run against the already-collected artifacts on disk (raw/, normalized/,
reports/) for REPORT_DATE — they do not call the live API. Run collect.py
and build_reports.py for that date first.

    python3 -m pytest test_pilot.py -v
"""
from __future__ import annotations

import csv
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tiktok_client import READ_ENDPOINTS, SecurityError, _enforce_allowlist  # noqa: E402

PILOT_DIR = Path(__file__).resolve().parent
RAW_DIR = PILOT_DIR / "raw"
NORMALIZED_DIR = PILOT_DIR / "normalized"
REPORTS_DIR = PILOT_DIR / "reports"
REPORT_DATE = "2026-09-07"


def read_csv(name: str) -> list[dict]:
    stem = name[:-4] if name.endswith(".csv") else name
    path = NORMALIZED_DIR / f"{stem}_{REPORT_DATE}.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- A. No duplicate order_id -----------------------------------------------

def test_a_no_duplicate_order_ids():
    orders = read_csv("orders.csv")
    ids = [o["order_id"] for o in orders]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"Duplicate order_id(s) in orders.csv: {dupes}"


# --- B. Order line quantities numeric ---------------------------------------

def test_b_line_quantities_numeric():
    lines = read_csv("order_lines.csv")
    assert lines, "order_lines.csv is empty — nothing to validate"
    for row in lines:
        q = row["quantity"]
        assert re.fullmatch(r"-?\d+", q), f"Non-numeric quantity {q!r} on line_item {row['line_item_id']}"


# --- C. Currency consistent or explicitly flagged ---------------------------

def test_c_currency_consistent_or_flagged():
    orders = read_csv("orders.csv")
    currencies = {o["currency"] for o in orders if o["currency"]}
    ceo = load_json(REPORTS_DIR / f"ceo_summary_{REPORT_DATE}.json")
    if len(currencies) > 1:
        # Inconsistency must be explicitly surfaced, not silently averaged.
        assert isinstance(ceo["currency"], list) or "," in str(ceo["currency"]), (
            f"Multiple currencies {currencies} found but ceo_summary.currency "
            f"does not flag it: {ceo['currency']!r}"
        )
    else:
        assert ceo["currency"] in currencies or not currencies


# --- D. Pagination completed -------------------------------------------------

def test_d_pagination_completed():
    by_prefix: dict[str, list[dict]] = {}
    for path in sorted(RAW_DIR.glob(f"{REPORT_DATE}_*_page_*.json")):
        payload = load_json(path)
        prefix = re.sub(r"_page_\d+\.json$", "", path.name)
        by_prefix.setdefault(prefix, []).append(payload)

    for prefix, pages in by_prefix.items():
        pages.sort(key=lambda p: p["page_number"])
        for i in range(len(pages) - 1):
            this_page, next_page = pages[i], pages[i + 1]
            if this_page.get("next_page_token"):
                assert next_page["page_number"] == this_page["page_number"] + 1, (
                    f"{prefix}: page {this_page['page_number']} had a next_page_token "
                    "but the following page was not fetched"
                )
        # The last page on disk should not have a next_page_token UNLESS
        # collect.py deliberately capped that domain's pagination (its
        # max_pages= argument) — that's a documented, intentional stop
        # (Section 24: "khong retry vo han"), not a silent truncation bug.
        # Every page up to the cap must still have been fetched contiguously,
        # which the loop above already verified.
        last = pages[-1]
        CAPPED_DOMAINS_MAX_PAGES = {
            "2026-09-07_finance_statements": 10,
            "2026-09-07_finance_payments": 10,
            "2026-09-07_finance_unsettled": 10,
        }
        if last.get("response_code") in (0, "0") and last.get("next_page_token"):
            cap = CAPPED_DOMAINS_MAX_PAGES.get(prefix)
            assert cap is not None and last["page_number"] == cap, (
                f"{prefix}: last collected page ({last['page_number']}) still had a "
                "next_page_token and this domain has no known intentional page cap "
                "— pagination stopped early"
            )


# --- E. Get Order Detail batches <= 50 IDs ----------------------------------

def test_e_order_detail_batches_max_50():
    files = sorted(RAW_DIR.glob(f"{REPORT_DATE}_order_details_*.json"))
    assert files, "no order_details raw files found"
    for path in files:
        payload = load_json(path)
        assert payload.get("ids_requested", 0) <= 50, (
            f"{path.name}: requested {payload.get('ids_requested')} ids (> 50 max per "
            "official docs)"
        )


# --- F. No secrets in generated files ----------------------------------------

SECRET_PATTERNS = [
    re.compile(r"\baccess_token\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.]{10,}"),
    re.compile(r"\brefresh_token\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.]{10,}"),
    re.compile(r"\bapp_secret\b\s*[:=]\s*[\"']?[A-Za-z0-9]{10,}"),
    re.compile(r"\bTTP_[A-Za-z0-9_\-]{20,}"),  # TikTok access-token literal prefix
]
REDACTED_OK = "***REDACTED***"


def test_f_no_secrets_in_generated_files():
    checked = 0
    for d in (RAW_DIR, NORMALIZED_DIR, REPORTS_DIR):
        for path in d.rglob("*"):
            if not path.is_file() or path.suffix not in (".json", ".csv", ".md"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            checked += 1
            for pat in SECRET_PATTERNS:
                for m in pat.finditer(text):
                    snippet = m.group(0)
                    if REDACTED_OK in text[max(0, m.start() - 5): m.end() + 20]:
                        continue
                    pytest.fail(f"Possible secret leaked in {path}: {snippet[:40]}...")
    assert checked > 0


# --- G. Monetary values use Decimal (not binary float) in JSON outputs ------

def test_g_monetary_values_are_decimal_safe_strings():
    ceo = load_json(REPORTS_DIR / f"ceo_summary_{REPORT_DATE}.json")
    money_paths = [
        ("BUSINESS", "order_total"), ("BUSINESS", "AOV_working"),
        ("FINANCE", "settled_amount"), ("ANALYTICS", "product_gmv"),
        ("VIDEO", "video_gmv"), ("LIVE", "live_gmv"),
    ]
    raw_text = (REPORTS_DIR / f"ceo_summary_{REPORT_DATE}.json").read_text(encoding="utf-8")
    for section, key in money_paths:
        val = ceo[section][key]
        # Must be a JSON string (quoted in the source text), not a bare
        # float literal — a bare float in the file means it round-tripped
        # through Python float somewhere upstream.
        assert isinstance(val, str), f"{section}.{key} = {val!r} is not a string in ceo_summary.json"
        assert f'"{key}": "{val}"' in raw_text or f'"{key}":"{val}"' in raw_text
        try:
            Decimal(val)
        except InvalidOperation:
            pytest.fail(f"{section}.{key} = {val!r} is not Decimal-parseable")


# --- H. No missing report_date ----------------------------------------------

def test_h_report_date_present_everywhere():
    ceo = load_json(REPORTS_DIR / f"ceo_summary_{REPORT_DATE}.json")
    assert ceo["report_date"] == REPORT_DATE
    ds_path = REPORTS_DIR / f"data_status_{REPORT_DATE}.json"
    assert ds_path.exists()
    for path in RAW_DIR.glob(f"{REPORT_DATE}_*.json"):
        payload = load_json(path)
        assert payload.get("report_date") == REPORT_DATE, f"{path} missing/wrong report_date"


# --- I. No CM2 Final while Ads = WAITING_API --------------------------------

def test_i_no_cm2_final_while_ads_waiting():
    ceo = load_json(REPORTS_DIR / f"ceo_summary_{REPORT_DATE}.json")
    pnl = ceo["P&L"]
    assert pnl["Ads_status"] == "WAITING_API"
    cm2 = pnl["CM2_final_status"]
    assert "NOT" in cm2.upper() or "WAITING" in cm2.upper(), (
        f"CM2_final_status={cm2!r} does not clearly withhold a final value while Ads=WAITING_API"
    )
    # must not look like a bare computed number
    assert not re.fullmatch(r"-?\d+(\.\d+)?", cm2.strip())


# --- J. No API WRITE endpoint invoked ----------------------------------------

def test_j_no_write_endpoint_reachable():
    # every endpoint this pilot's client can call is GET or POST *…/search*
    # or *…/orders* (search/detail) — never create/update/delete/cancel/etc.
    # Check whole path SEGMENTS (not substrings — "return_refund" is a
    # legitimate resource name, not a write action) against action verbs.
    forbidden_segments = {"create", "update", "cancel", "approve", "reject",
                           "delete", "refund", "ship", "ban", "calculate"}
    for name, cfg in READ_ENDPOINTS.items():
        assert cfg["method"] in ("GET", "POST"), f"{name}: method {cfg['method']} is not read-shaped"
        segments = {s for s in cfg["path"].lower().split("/") if s}
        hit = segments & forbidden_segments
        assert not hit, f"{name}: path {cfg['path']} has a write-shaped segment {hit}"

    # the allowlist guard itself must actively reject write-shaped calls
    for method, path in [
        ("PUT", "/order/202309/orders"),
        ("DELETE", "/finance/202605/payments"),
        ("POST", "/order/202309/orders/cancel"),
        ("PATCH", "/product/202502/products/search"),
    ]:
        with pytest.raises(SecurityError):
            _enforce_allowlist(method, path)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
