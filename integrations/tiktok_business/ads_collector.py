"""P15 — TikTok API for Business (Marketing API) ads-cost collector.

Separate app/credentials from the TikTok Shop Open API app. Two cost
sources, kept apart by ``attribution_basis`` in core.fact_ads_daily:

* GMV_MAX — /gmv_max/report/get, filtered to the pipeline's own TikTok
  shop (store_id == TikTok shop_id). This is the shop-attributed ad cost
  that feeds Gold ``ads_spend`` / CM2.
* AUCTION — /report/integrated/get (AUCTION_CAMPAIGN). Not tied to a
  store by the API, so it is stored with shop_id NULL and NOT counted in
  ``ads_spend`` until HH decides how brand/auction ads are allocated.

Plus one COVERAGE row per (store, day) so Gold can tell "collected, zero
spend" apart from "not collected" (never invent 0 for a missing day).

Credentials (never logged, never written to disk):
  GitHub runner: TIKTOK_BUSINESS_AUTH_JSON = {"app_id","secret","access_token"}
  Mac local:     Keychain HH_TIKTOK_BUSINESS_API / HH_TIKTOK_BUSINESS_TOKEN
                 (account = app_id from HH_TIKTOK_BUSINESS_APP_ID env or the
                 single Keychain item's account)
Advertiser IDs are discovered at runtime (/oauth2/advertiser/get) — no
account inventory is committed to this public repo.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Optional

BASE_URL = "https://business-api.tiktok.com/open_api/v1.3"
GMV_MAX_ENDPOINT = "/gmv_max/report/get/"
AUCTION_ENDPOINT = "/report/integrated/get/"
COVERAGE_CAMPAIGN_ID = "__COVERAGE__"
TRANSIENT_CODES = {40100, 50000, 50002, 51021}  # rate limit / internal / timeout
PAGE_SIZE = 100  # max for /gmv_max/* and /campaign/get; /report/integrated/get allows 1000


class TikTokBusinessError(RuntimeError):
    def __init__(self, code, message, request_id=None):
        super().__init__(f"TikTok Business API error {code}: {message} (request_id {request_id})")
        self.code = code


# ---------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class Credentials:
    app_id: str
    secret: str = field(repr=False)
    access_token: str = field(repr=False)


def _keychain(service: str, account: Optional[str]) -> Optional[str]:
    cmd = ["security", "find-generic-password", "-s", service]
    if account:
        cmd += ["-a", account]
    cmd.append("-w")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:  # not macOS
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _keychain_account(service: str) -> Optional[str]:
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith('"acct"<blob>="'):
            return line.split('="', 1)[1].rstrip('"')
    return None


def load_credentials(env=None) -> Credentials:
    env = os.environ if env is None else env
    bundle = env.get("TIKTOK_BUSINESS_AUTH_JSON")
    if bundle:
        data = json.loads(bundle)
        missing = [k for k in ("app_id", "secret", "access_token") if not data.get(k)]
        if missing:
            raise RuntimeError(f"TIKTOK_BUSINESS_AUTH_JSON missing keys: {missing}")
        return Credentials(str(data["app_id"]), data["secret"], data["access_token"])
    app_id = env.get("HH_TIKTOK_BUSINESS_APP_ID") or _keychain_account("HH_TIKTOK_BUSINESS_TOKEN")
    token = _keychain("HH_TIKTOK_BUSINESS_TOKEN", app_id)
    secret = _keychain("HH_TIKTOK_BUSINESS_API", app_id)
    if not (app_id and token and secret):
        raise RuntimeError("TikTok Business credentials not found (TIKTOK_BUSINESS_AUTH_JSON or Keychain)")
    return Credentials(app_id, secret, token)


# ---------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------

def make_getter(creds: Credentials, log: Callable[[str], None], sleep=time.sleep, http_get=None):
    """Returns get(path, params) -> data dict. Retries transient codes/HTTP."""
    if http_get is None:
        import requests

        def http_get(url, params, headers):
            r = requests.get(url, params=params, headers=headers, timeout=60)
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, {"code": -1, "message": r.text[:200]}

    def get(path: str, params: dict) -> dict:
        q = {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}
        delays = (5, 15, 45)
        for attempt in range(len(delays) + 1):
            status, body = http_get(BASE_URL + path, q, {"Access-Token": creds.access_token})
            code = body.get("code")
            if status == 200 and code == 0:
                return body.get("data") or {}
            transient = status in (429, 500, 502, 503, 504) or code in TRANSIENT_CODES
            if transient and attempt < len(delays):
                log(f"tiktok business {path}: transient {status}/{code}, retry in {delays[attempt]}s")
                sleep(delays[attempt])
                continue
            raise TikTokBusinessError(code if code is not None else status, body.get("message"), body.get("request_id"))
        raise AssertionError("unreachable")

    return get


def paginate(get, path: str, params: dict, page_size: int = PAGE_SIZE) -> list:
    rows, page = [], 1
    while True:
        data = get(path, dict(params, page=page, page_size=page_size))
        rows += data.get("list") or []
        info = data.get("page_info") or {}
        if page >= (info.get("total_page") or 1):
            return rows
        page += 1


# ---------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------

def discover_advertisers(get, creds: Credentials) -> list:
    data = get("/oauth2/advertiser/get/", {"app_id": creds.app_id, "secret": creds.secret})
    return [str(a["advertiser_id"]) for a in data.get("list") or []]


def advertisers_for_store(get, advertiser_ids: Iterable[str], store_id: str) -> list:
    """Advertisers whose GMV Max store list contains this store. Historic
    GMV Max campaigns can sit on advertisers that are no longer the
    store's exclusive one, so every advertiser that can see the store is
    queried (their report rows are per-advertiser — no double count)."""
    out = []
    for adv in advertiser_ids:
        stores = get("/gmv_max/store/list/", {"advertiser_id": adv}).get("store_list") or []
        if any(str(s.get("store_id")) == str(store_id) for s in stores):
            out.append(adv)
    return out


def campaign_names(get, advertiser_id: str) -> dict:
    rows = paginate(get, "/gmv_max/campaign/get/", {
        "advertiser_id": advertiser_id,
        "filtering": {"gmv_max_promotion_types": ["PRODUCT_GMV_MAX", "LIVE_GMV_MAX"]},
    })
    return {str(r["campaign_id"]): r.get("campaign_name") for r in rows}


# ---------------------------------------------------------------------
# pure normalisation (unit-tested)
# ---------------------------------------------------------------------

def dec(v) -> Optional[Decimal]:
    if v is None or v == "" or v == "-":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _int(v) -> Optional[int]:
    d = dec(v)
    return int(d) if d is not None else None


def _day(stat_time_day: str) -> date:
    return date.fromisoformat(str(stat_time_day)[:10])


def normalize_gmv_max(rows: list, advertiser_id: str, store_id: str, names: dict) -> list:
    out = []
    for r in rows:
        dims, m = r.get("dimensions") or {}, r.get("metrics") or {}
        cid = str(dims.get("campaign_id"))
        cost, revenue = dec(m.get("cost")), dec(m.get("gross_revenue"))
        if not cost and not revenue and not _int(m.get("orders")):
            continue  # report returns a zero row for every campaign x day, even idle ones
        out.append({
            "ad_account_id": advertiser_id, "shop_id": store_id, "campaign_id": cid,
            "campaign_name": names.get(cid), "business_date": _day(dims.get("stat_time_day")),
            "impressions": None, "clicks": None, "spend": cost, "net_spend": dec(m.get("net_cost")),
            "orders": _int(m.get("orders")), "gmv": revenue,
            "roas": round(revenue / cost, 4) if cost and revenue is not None else None,
            "ctr": None, "conversion": None, "attribution_basis": "GMV_MAX",
            "source_endpoint": GMV_MAX_ENDPOINT,
        })
    return out


def normalize_auction(rows: list, advertiser_id: str) -> list:
    out = []
    for r in rows:
        dims, m = r.get("dimensions") or {}, r.get("metrics") or {}
        spend = dec(m.get("spend"))
        if not spend and not _int(m.get("impressions")):
            continue  # report returns zero rows for idle campaigns/days
        impressions, clicks = _int(m.get("impressions")), _int(m.get("clicks"))
        out.append({
            "ad_account_id": advertiser_id, "shop_id": None, "campaign_id": str(dims.get("campaign_id")),
            "campaign_name": m.get("campaign_name"), "business_date": _day(dims.get("stat_time_day")),
            "impressions": impressions, "clicks": clicks, "spend": spend, "net_spend": None,
            "orders": None, "gmv": None, "roas": None,
            "ctr": round(Decimal(clicks) / impressions, 4) if impressions and clicks is not None else None,
            "conversion": _int(m.get("conversion")), "attribution_basis": "AUCTION",
            "source_endpoint": AUCTION_ENDPOINT,
        })
    return out


def coverage_rows(store_id: str, days: Iterable[date]) -> list:
    return [{
        "ad_account_id": f"STORE:{store_id}", "shop_id": store_id, "campaign_id": COVERAGE_CAMPAIGN_ID,
        "campaign_name": "collector coverage marker (not spend)", "business_date": d,
        "impressions": None, "clicks": None, "spend": Decimal(0), "net_spend": Decimal(0),
        "orders": None, "gmv": None, "roas": None, "ctr": None, "conversion": None,
        "attribution_basis": "COVERAGE", "source_endpoint": GMV_MAX_ENDPOINT,
    } for d in days]


# ---------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------

def collect(get, creds: Credentials, store_id: str, start: date, end: date, log) -> list:
    """All TikTok ads rows for [start, end] (inclusive, advertiser timezone
    = Asia/Ho_Chi_Minh for every HH account). Raises on any API failure —
    a partial pull must never be written as if complete."""
    advertisers = discover_advertisers(get, creds)
    s, e = start.isoformat(), end.isoformat()
    rows = []
    for adv in advertisers_for_store(get, advertisers, store_id):
        names = campaign_names(get, adv)
        gm = paginate(get, GMV_MAX_ENDPOINT, {
            "advertiser_id": adv, "store_ids": [store_id], "start_date": s, "end_date": e,
            "dimensions": ["campaign_id", "stat_time_day"],
            "metrics": ["cost", "net_cost", "orders", "gross_revenue"],
        })
        rows += normalize_gmv_max(gm, adv, store_id, names)
    for adv in advertisers:
        au = paginate(get, AUCTION_ENDPOINT, {
            "advertiser_id": adv, "report_type": "BASIC", "data_level": "AUCTION_CAMPAIGN",
            "dimensions": ["campaign_id", "stat_time_day"],
            "metrics": ["campaign_name", "spend", "impressions", "clicks", "conversion"],
            "start_date": s, "end_date": e,
        }, page_size=1000)
        rows += normalize_auction(au, adv)
    days, d = [], start
    while d <= end:
        days.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    rows += coverage_rows(store_id, days)
    log(f"tiktok ads {s}..{e}: advertisers={len(advertisers)} rows={len(rows)}")
    return rows


# ---------------------------------------------------------------------
# DB write (shared by the incremental worker and the backfill script)
# ---------------------------------------------------------------------

def upsert_rows(cur, etl_run_id, rows, ctr) -> dict:
    """Idempotent upsert into core.fact_ads_daily (requires sql/066 net_spend).
    ctr: incr_common.Counters-like object with record(inserted: bool)/as_dict()."""
    for r in rows:
        cur.execute(
            """
            INSERT INTO core.fact_ads_daily (
                channel, ad_account_id, shop_id, campaign_id, campaign_name, business_date,
                impressions, clicks, spend, net_spend, orders, gmv, ctr, roas, conversion,
                attribution_basis, source_system, source_endpoint, source_shop_id, etl_run_id
            ) VALUES ('TIKTOK',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'TIKTOK_BUSINESS',%s,%s,%s)
            ON CONFLICT (channel, ad_account_id, business_date, campaign_id) DO UPDATE SET
                campaign_name = EXCLUDED.campaign_name, shop_id = EXCLUDED.shop_id,
                impressions = EXCLUDED.impressions, clicks = EXCLUDED.clicks,
                spend = EXCLUDED.spend, net_spend = EXCLUDED.net_spend,
                orders = EXCLUDED.orders, gmv = EXCLUDED.gmv, ctr = EXCLUDED.ctr,
                roas = EXCLUDED.roas, conversion = EXCLUDED.conversion,
                attribution_basis = EXCLUDED.attribution_basis,
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING (xmax = 0) AS inserted;
            """,
            (
                r["ad_account_id"], r["shop_id"], r["campaign_id"], r["campaign_name"], r["business_date"],
                r["impressions"], r["clicks"], r["spend"], r["net_spend"], r["orders"], r["gmv"],
                r["ctr"], r["roas"], r["conversion"], r["attribution_basis"],
                r["source_endpoint"], r["shop_id"], etl_run_id,
            ),
        )
        ctr.record(cur.fetchone()[0])
    return ctr.as_dict()
