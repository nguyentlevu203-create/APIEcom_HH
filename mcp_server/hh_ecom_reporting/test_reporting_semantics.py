"""
P14-B/P14-C — the P8.0 Python reference must give exactly the Worker's answers for
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
        e = freshness_envelope(i["fromDate"], i["toDate"], i["rowCount"], i["firstLoadedDate"],
                               i["lastLoadedDate"], i["coverage"], "x")
        assert (e["status"], e["source_freshness_status"]) == (c["status"], c["source_freshness_status"]), c["case"]


def test_freshness_envelope_has_one_active_path():
    """P14-C — no statement may follow an unconditional top-level return
    (the obsolete duplicate D4 block lived there)."""
    import ast
    tree = ast.parse((HERE / "reporting_semantics.py").read_text())
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        returns = [i for i, st in enumerate(fn.body) if isinstance(st, ast.Return)]
        assert not returns or returns[0] == len(fn.body) - 1, f"dead code after return in {fn.name}"
    assert (HERE / "reporting_semantics.py").read_text().count("def freshness_envelope(") == 1


def test_reference_has_no_local_ready_for_any_non_null_cost():
    src = (HERE / "query_service.py").read_text()
    assert "def _cost_status(" not in src and " _cost_status(" not in src
    assert '"READY" if r["total_cogs"] is not None' not in src


def test_reference_cogs_status_aggregation_is_not_text_max():
    src = (HERE / "query_service.py").read_text()
    assert "max(availability_status)" not in src
    assert "bool_or(availability_status = 'COGS_INCOMPLETE')" in src
