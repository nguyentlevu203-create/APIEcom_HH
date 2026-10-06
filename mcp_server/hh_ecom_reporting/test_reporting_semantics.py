"""
P14-B — the P8.0 Python reference must give exactly the Worker's answers for
the shared D2/D3/D4 cases (workers/hh-ecom-reporting-mcp/test/parity_vectors.json).
Pure functions only, no DB. Run: python3 -m pytest mcp_server/hh_ecom_reporting -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from reporting_semantics import cogs_envelope, cost_line, freshness_envelope  # noqa: E402

V = json.loads((HERE.parent.parent / "workers" / "hh-ecom-reporting-mcp" / "test" / "parity_vectors.json").read_text())


def test_cost_line_matches_worker():
    for c in V["cost_line"]:
        assert cost_line(*c["args"]) == (c["amount"], c["status"]), c["args"]


def test_cogs_envelope_matches_worker():
    for c in V["cogs"]:
        assert cogs_envelope(*c["args"]) == {"value": c["value"], "status": c["status"]}, c["args"]


def test_freshness_envelope_matches_worker():
    for c in V["freshness"]:
        i = c["in"]
        e = freshness_envelope(i["fromDate"], i["toDate"], i["rowCount"], i["latestAvailableDate"], i["coverage"], "x")
        assert (e["status"], e["source_freshness_status"]) == (c["status"], c["source_freshness_status"]), i


def test_reference_has_no_local_ready_for_any_non_null_cost():
    src = (HERE / "query_service.py").read_text()
    assert "def _cost_status(" not in src and " _cost_status(" not in src
    assert '"READY" if r["total_cogs"] is not None' not in src


def test_reference_cogs_status_aggregation_is_not_text_max():
    src = (HERE / "query_service.py").read_text()
    assert "max(availability_status)" not in src
    assert "bool_or(availability_status = 'COGS_INCOMPLETE')" in src
