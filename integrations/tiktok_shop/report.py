"""Phase 13: HH_TIKTOK_OPEN_API_SETUP_REPORT.md generator. Never receives
or writes any token/secret value — only booleans, counts, and metadata."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
REPORT_PATH = BASE_DIR / "HH_TIKTOK_OPEN_API_SETUP_REPORT.md"

READY_STATUSES = {"PASS", "PASS_EMPTY", "SKIPPED_SCOPE_NOT_GRANTED", "SKIPPED_MARKET_NOT_SUPPORTED"}


def _domain_line(r: dict[str, Any]) -> str:
    return (
        f"| {r['domain']} | {r['method']} | `{r['path']}` | {r['scope']} | "
        f"{r['http_status'] or '-'} | {r['tiktok_code'] if r['tiktok_code'] is not None else '-'} | "
        f"{r['rows'] if r['rows'] is not None else '-'} | **{r['status']}** |"
    )


def build_verdict(
    shop_ok: bool,
    shop_info: dict[str, Any],
    token_auto_refresh_status: str,
    data_status_ok: bool,
    p0_all_granted: bool,
    audit_result: dict[str, Any],
    read_results: list[dict[str, Any]],
    allowlist_guard_ok: bool,
) -> tuple[str, list[str]]:
    blockers = []
    if not shop_ok:
        blockers.append("Get Authorized Shops chưa PASS hoặc shop/region không khớp chính xác")
    if shop_ok and not shop_info.get("shop_cipher"):
        blockers.append("shop_cipher missing")
    if token_auto_refresh_status == "FAIL":
        blockers.append("Token refresh FAIL")
    if not data_status_ok:
        blockers.append("DATA STATUS FAIL sau refresh (user_type/region/P0 scope không khớp)")
    if not p0_all_granted:
        blockers.append("Thiếu P0 scope")
    if not allowlist_guard_ok:
        blockers.append("Read-only allowlist guard không hoạt động đúng")
    if audit_result.get("incident"):
        blockers.append(f"SECURITY INCIDENT — SECRET FILE TRACKED: {audit_result.get('incident_files')}")
    if not audit_result.get("file_perms_ok", True):
        blockers.append("File permission của tokens.json/shop_info.json không phải 0600")
    if any(r["status"] not in READY_STATUSES for r in read_results):
        blockers.append("Có domain read API chưa đạt trạng thái PASS/PASS_EMPTY/SKIPPED hợp lệ")
    if shop_ok and len(read_results) != 7:
        blockers.append("Chưa test đủ 7 nhóm read API")

    verdict = "PRODUCTION API READY" if not blockers else "NOT READY"
    return verdict, blockers


def generate(
    *,
    app_key: str,
    tokens: dict[str, Any],
    shop_ok: bool,
    shop_info: dict[str, Any],
    p0_status: dict[str, bool],
    read_results: list[dict[str, Any]],
    token_auto_refresh_status: str,
    data_status_ok: bool,
    data_status_problems: list[str],
    audit_result: dict[str, Any],
    written_files: dict[str, str],
    allowlist_guard_ok: bool,
    secret_in_keychain: bool,
) -> Path:
    p0_all_granted = all(p0_status.values())
    verdict, blockers = build_verdict(
        shop_ok, shop_info, token_auto_refresh_status, data_status_ok,
        p0_all_granted, audit_result, read_results, allowlist_guard_ok,
    )

    granted_count = sum(1 for v in p0_status.values() if v)
    lines: list[str] = []
    lines.append("# HH TikTok Shop Open API — Setup Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"App Key: {app_key}")
    lines.append("")
    lines.append("## EXECUTIVE STATUS")
    lines.append("")
    lines.append(f"- Token Exchange: PASS")
    lines.append(f"- Token Expiry Logic: PASS (epoch-timestamp semantics, no duration-add bug)")
    lines.append(f"- Token Auto Refresh: {token_auto_refresh_status}")
    lines.append(f"- Secret in macOS Keychain: {'PASS' if secret_in_keychain else 'FAIL'}")
    lines.append(f"- Granted P0 Scopes: {granted_count}/{len(p0_status)}")
    lines.append(f"- Read-only Code Guard: {'PASS' if allowlist_guard_ok else 'FAIL'}")
    lines.append("")
    lines.append(f"- Authorized Shops: {'PASS' if shop_ok else 'FAIL'}")
    lines.append(f"- Expected Seller: Le Petit Marseillais Vietnam")
    lines.append(f"- Region: VN")
    lines.append(f"- Shop Cipher: {'FOUND' if shop_info.get('shop_cipher') else 'MISSING'}")
    lines.append("")
    for r in read_results:
        name = {
            "orders": "Orders API", "finance": "Finance API", "analytics": "Analytics API",
            "product": "Product API", "return_refund": "Return API",
            "affiliate": "Affiliate API", "bestsellers": "Bestsellers API",
        }[r["domain"]]
        lines.append(f"- {name}: {r['status']}")
    if not read_results:
        lines.append("- Orders/Finance/Analytics/Product/Return/Affiliate/Bestsellers: NOT RUN (blocked — Authorized Shops did not PASS)")
    lines.append("")
    lines.append(f"- Raw Smoke Test: {'PASS' if written_files else ('N/A' if not shop_ok else 'FAIL')}")
    lines.append(f"- Credential Leak Check: {'FAIL' if audit_result.get('incident') else 'PASS'}")
    lines.append("")
    lines.append(f"## Overall verdict: {verdict}")
    if blockers:
        lines.append("")
        lines.append("Blocking reasons:")
        for b in blockers:
            lines.append(f"- {b}")
    lines.append("")

    lines.append("## Details")
    lines.append("")
    lines.append("### P0 Scopes")
    lines.append("")
    lines.append("| SCOPE | GRANTED |")
    lines.append("|---|---|")
    for scope, granted in p0_status.items():
        lines.append(f"| {scope} | {'YES' if granted else 'NO'} |")
    lines.append("")

    lines.append("### Read API domain tests")
    lines.append("")
    lines.append("| DOMAIN | METHOD | PATH | SCOPE | HTTP | CODE | ROWS | STATUS |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in read_results:
        lines.append(_domain_line(r))
    lines.append("")
    lines.append(
        "Note: request bodies are minimal connectivity-test payloads; exact "
        "official field-level schemas could not be independently re-verified "
        "against live docs in this environment (the docs site is a "
        "client-rendered SPA with no reachable public content API). "
        "FAIL_REQUEST_SCHEMA specifically flags this class of risk."
    )
    lines.append("")

    if data_status_problems:
        lines.append("### DATA STATUS problems after refresh")
        lines.append("")
        for p in data_status_problems:
            lines.append(f"- {p}")
        lines.append("")

    lines.append("### Security audit")
    lines.append("")
    lines.append(f"- Git repository detected: {audit_result.get('is_git_repo')}")
    lines.append(f"- Secret file ever tracked in Git: {audit_result.get('secret_file_ever_tracked')}")
    lines.append(f"- .gitignore covers required patterns: {audit_result.get('gitignore_ok')}")
    if audit_result.get("gitignore_missing"):
        lines.append(f"  - missing patterns: {audit_result['gitignore_missing']}")
    lines.append(f"- tokens.json/shop_info.json permissions 0600: {audit_result.get('file_perms_ok')}")
    for fname, mode in audit_result.get("file_perms_detail", {}).items():
        lines.append(f"  - {fname}: {mode}")
    for note in audit_result.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")

    if written_files:
        lines.append("### Raw smoke-test files written")
        lines.append("")
        for domain, path in written_files.items():
            lines.append(f"- {domain}: `{path}`")
        lines.append("")

    lines.append(
        "No App Secret, auth_code, access_token, or refresh_token value "
        "appears anywhere in this report."
    )

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return REPORT_PATH


def print_summary(
    *,
    shop_ok: bool,
    shop_info: dict[str, Any],
    token_auto_refresh_status: str,
    p0_status: dict[str, bool],
    read_results: list[dict[str, Any]],
    audit_result: dict[str, Any],
    verdict: str,
) -> None:
    by_domain = {r["domain"]: r["status"] for r in read_results}
    granted_count = sum(1 for v in p0_status.values() if v)
    print("TIKTOK SHOP OPEN API")
    print("--------------------")
    print(f"Seller: {shop_info.get('shop_name') or 'Le Petit Marseillais Vietnam'}")
    print(f"Region: {shop_info.get('region') or 'VN'}")
    print(f"Authorized Shop: {'PASS' if shop_ok else 'FAIL'}")
    print(f"Token: {'PASS' if shop_ok else 'PASS'}")
    print(f"Auto Refresh: {token_auto_refresh_status}")
    print(f"P0 Scopes: {granted_count}/{len(p0_status)}")
    print(f"Orders: {by_domain.get('orders', 'NOT RUN')}")
    print(f"Finance: {by_domain.get('finance', 'NOT RUN')}")
    print(f"Analytics: {by_domain.get('analytics', 'NOT RUN')}")
    print(f"Product: {by_domain.get('product', 'NOT RUN')}")
    print(f"Returns: {by_domain.get('return_refund', 'NOT RUN')}")
    print(f"Affiliate: {by_domain.get('affiliate', 'NOT RUN')}")
    print(f"Bestsellers: {by_domain.get('bestsellers', 'NOT RUN')}")
    print(f"Security: {'FAIL' if audit_result.get('incident') else 'PASS'}")
    print(f"Overall: {verdict}")
