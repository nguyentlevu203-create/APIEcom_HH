#!/usr/bin/env python3
"""
P8.0 — HH Ecom read-only reporting MCP server.

Exposes exactly 7 tools, each a thin wrapper around one function in
query_service.py. No tool here accepts, builds, or executes arbitrary
SQL — every tool takes a small set of validated, typed parameters and
returns a JSON-safe dict. This file contains no SQL text at all.

Transport: streamable-HTTP (suitable for a Secure MCP Tunnel or a
reverse-proxied HTTPS deployment) when run directly; the underlying
FastMCP instance can also be driven over stdio for local
integration testing (see run_local_stdio_smoke_test in this project's
test script, not in this file).

Auth: bearer-token check via the MCP_AUTH_TOKEN environment variable —
never hard-coded. If unset, the server refuses to start (fails closed).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import query_service as qs  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

AUTH_ENV_VAR = "MCP_AUTH_TOKEN"

mcp = FastMCP(
    name="hh-ecom-reporting",
    instructions=(
        "Read-only HH Ecom reporting tools (Shopee + TikTok). All data comes from "
        "approved mart.v_ai_*/v_ceo_* views via the hh_ai_reader role. "
        "ALWAYS check status/value_basis/blocking_reason fields before stating a "
        "number as fact — a NULL value with a status field explains why, it is "
        "never a real zero. Platform GMV is never Net Sales. TikTok LIVE "
        "account_type='ALL' is the authoritative total; 'AFFILIATE_ACCOUNTS' is a "
        "SUBSET of it — never sum the two. Before concluding CM2 is complete or a "
        "channel/campaign is profitable at CM2, call get_data_coverage and "
        "get_cost_breakdown and confirm no material cost source is missing; if any "
        "is, say so explicitly (in Vietnamese if the user asked in Vietnamese: "
        "'CHƯA ĐỦ DỮ LIỆU ĐỂ KẾT LUẬN CM2 HOÀN CHỈNH') and list exactly what is missing."
    ),
)


def _wrap(fn):
    """Translate this module's ValidationError into a safe MCP tool error
    without ever leaking a raw DB exception message to the client."""
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except qs.ValidationError as e:
            return {"error": "invalid_input", "message": str(e)}
        except RuntimeError as e:
            return {"error": "query_failed", "message": str(e)}
    inner.__name__ = fn.__name__
    inner.__doc__ = fn.__doc__
    return inner


@mcp.tool()
def get_ecom_overview(from_date: str, to_date: str, platform: Optional[str] = None) -> dict:
    """Daily Sales/Net Sales/COGS/GM1/CM1/CM2/Profit overview per platform.
    from_date/to_date: YYYY-MM-DD, max 31-day range. platform: SHOPEE or TIKTOK (omit for both).
    Platform GMV and Net Sales are always returned as separate fields — never conflate them."""
    return _wrap(qs.get_ecom_overview)(from_date, to_date, platform)


@mcp.tool()
def get_cost_breakdown(from_date: str, to_date: str, platform: Optional[str] = None) -> dict:
    """Every currently verified cost component (fees, packaging, affiliate commission,
    Ads spend, Booking/KOL/KOC, Live in-house cost, backoffice) with amount=NULL + a real
    status whenever a source is unavailable. Never returns a fabricated zero."""
    return _wrap(qs.get_cost_breakdown)(from_date, to_date, platform)


@mcp.tool()
def get_operations(platform: str, date: Optional[str] = None) -> dict:
    """Shopee Account Health operational KPIs (Late Shipment Rate, Non-Fulfilment Rate,
    Shop Rating, listing violations, etc). Snapshot/current-state data only — Shopee's
    API has no historical backfill. platform must be SHOPEE or TIKTOK (TikTok returns
    an explanatory empty result — no violation-points API exists for TikTok)."""
    return _wrap(qs.get_operations)(platform, date)


@mcp.tool()
def get_video_performance(from_date: str, to_date: str, platform: str,
                           account_type: Optional[str] = None, limit: Optional[int] = None) -> dict:
    """TikTok video/content-grain performance (views, GMV, orders, CTR, creator).
    account_type: ALL or AFFILIATE_ACCOUNTS. Only proven for TikTok; Shopee returns
    an explanatory empty result. Never attaches shop-daily impressions/clicks that
    don't exist at this grain."""
    return _wrap(qs.get_video_performance)(from_date, to_date, platform, account_type, limit)


@mcp.tool()
def get_live_performance(from_date: str, to_date: str, account_type: Optional[str] = None,
                          limit: Optional[int] = None) -> dict:
    """TikTok LIVE session performance (GMV, orders, viewers, CTR, CTOR, engagement).
    account_type='ALL' (default) is the authoritative total slice; 'AFFILIATE_ACCOUNTS'
    is a SUBSET — never sum the two. Shopee LIVE has no data here (NO_PERMISSION)."""
    return _wrap(qs.get_live_performance)(from_date, to_date, account_type, limit)


@mcp.tool()
def get_affiliate_performance(from_date: str, to_date: str, platform: str, limit: Optional[int] = None) -> dict:
    """Affiliate/creator performance (sales, orders, commission, ROI). Proven for
    SHOPEE only — TikTok's affiliate performance lives at the video/LIVE content
    grain instead (call get_video_performance/get_live_performance with
    account_type='AFFILIATE_ACCOUNTS'); requesting platform=TIKTOK here returns an
    explanatory routing note, not fabricated data."""
    return _wrap(qs.get_affiliate_performance)(from_date, to_date, platform, limit)


@mcp.tool()
def get_data_coverage(from_date: Optional[str] = None, to_date: Optional[str] = None,
                       platform: Optional[str] = None) -> dict:
    """The locked 52-KPI coverage state (API_ACTUAL / DERIVED_VERIFIED / NO_PERMISSION /
    SEPARATE_API_REQUIRED / NOT_EXPOSED_PUBLIC_API / MISSING_SOURCE) plus live per-date
    metric status when a date range is given. Call this BEFORE making any claim about
    data completeness, especially before concluding CM2 is complete."""
    return _wrap(qs.get_data_coverage)(from_date, to_date, platform)


class _BearerAuthMiddleware:
    """Minimal, real bearer-token gate in front of the MCP streamable-HTTP
    app. Deliberately not the full OAuth AuthSettings/TokenVerifier path
    (that requires a public issuer_url this private pilot doesn't have) —
    this is a single static shared secret, read only from the
    MCP_AUTH_TOKEN environment variable, compared with a constant-time
    check. 401s on any missing/incorrect token before the request ever
    reaches a tool."""

    def __init__(self, app, expected_token: str):
        self.app = app
        self._expected = expected_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        import hmac
        provided = auth[7:] if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(provided, self._expected):
            from starlette.responses import JSONResponse
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def main() -> int:
    token = os.environ.get(AUTH_ENV_VAR)
    if not token:
        print(f"FATAL: {AUTH_ENV_VAR} is not set. Refusing to start an unauthenticated "
              "reporting server. Set it to a long random secret before running.", file=sys.stderr)
        return 1

    import uvicorn

    mcp.settings.streamable_http_path = "/mcp"
    app = mcp.streamable_http_app()
    secured_app = _BearerAuthMiddleware(app, token)

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8765"))
    uvicorn.run(secured_app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
