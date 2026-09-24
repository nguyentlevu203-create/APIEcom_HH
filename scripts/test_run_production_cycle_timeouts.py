"""
P11-QUATER Q6 — static invariant: the GitHub Actions job-level timeout
must stay strictly greater than the summed worst-case stage budgets
scripts/run_production_cycle.py can spend.

Proven live (P11-TER controlled run, 2026-09-22): with the job timeout
set equal to the stage-budget sum (90 min job vs. 3600s+1800s=90 min of
stage budget alone, before GOLD/setup/coverage/healthcheck/log-upload),
GitHub cancelled the job a few seconds before it would have finished
cleanly — not a code bug, a budget-sizing bug. This test exists so that
relationship can never silently regress again: it reads both sides from
their real source files (not hardcoded numbers here) and fails loudly
if a future edit to either file breaks the inequality.

Run with: python3 -m pytest scripts/test_run_production_cycle_timeouts.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CYCLE_SRC = (ROOT / "scripts" / "run_production_cycle.py").read_text(encoding="utf-8")
WORKFLOW_SRC = (ROOT / ".github" / "workflows" / "p3-incremental.yml").read_text(encoding="utf-8")
INCREMENTAL_SRC = (ROOT / "pipelines" / "incremental.py").read_text(encoding="utf-8")


def _int_constant(src: str, name: str) -> int:
    m = re.search(rf"^{name}\s*=\s*(\d+)", src, re.MULTILINE)
    assert m, f"could not find {name} in run_production_cycle.py"
    return int(m.group(1))


def _gold_chain_length(src: str) -> int:
    m = re.search(r"GOLD_CHAIN\s*=\s*\[(.*?)\n\]", src, re.DOTALL)
    assert m, "could not find GOLD_CHAIN list in run_production_cycle.py"
    # one tuple per line inside the list literal
    return len([line for line in m.group(1).splitlines() if line.strip().startswith("(")])


def _gold_overrides(src: str) -> dict:
    m = re.search(r"GOLD_SCRIPT_TIMEOUT_OVERRIDES\s*=\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, "could not find GOLD_SCRIPT_TIMEOUT_OVERRIDES in run_production_cycle.py"
    overrides = dict((k, int(v)) for k, v in re.findall(r'"([^"]+)"\s*:\s*(\d+)', m.group(1)))
    chain = re.search(r"GOLD_CHAIN\s*=\s*\[(.*?)\n\]", src, re.DOTALL).group(1)
    for script in overrides:
        assert f'"{script}"' in chain, f"override for {script} does not match any GOLD_CHAIN script"
    return overrides


def _job_timeout_minutes(src: str) -> int:
    # last (active, uncommented) timeout-minutes: wins — matches what
    # GitHub Actions itself parses.
    matches = re.findall(r"^\s*timeout-minutes:\s*(\d+)", src, re.MULTILINE)
    assert matches, "could not find an active timeout-minutes: in the workflow"
    return int(matches[-1])


def test_job_timeout_exceeds_summed_stage_budget():
    ingestion = _int_constant(CYCLE_SRC, "INGESTION_TIMEOUT_SECONDS")
    reconciliation = _int_constant(CYCLE_SRC, "RECONCILIATION_TIMEOUT_SECONDS")
    healthcheck = _int_constant(CYCLE_SRC, "HEALTHCHECK_TIMEOUT_SECONDS")
    gold_script = _int_constant(CYCLE_SRC, "GOLD_SCRIPT_TIMEOUT_SECONDS")
    gold_script_count = _gold_chain_length(CYCLE_SRC)
    gold_overrides = _gold_overrides(CYCLE_SRC)

    # Worst case: every stage independently uses its FULL budget. Real
    # runs are much faster (Gold scripts fail/succeed in seconds, not
    # 600s each) — this is deliberately the pessimistic ceiling, not a
    # typical-case estimate, because a timeout bound only means
    # something if it's sized against the worst case it's meant to catch.
    gold_total = gold_script * (gold_script_count - len(gold_overrides)) + sum(gold_overrides.values())
    stage_budget_seconds = ingestion + reconciliation + healthcheck + gold_total

    job_timeout_seconds = _job_timeout_minutes(WORKFLOW_SRC) * 60

    assert job_timeout_seconds > stage_budget_seconds, (
        f"GitHub job timeout ({job_timeout_seconds}s) must exceed the summed "
        f"worst-case stage budget ({stage_budget_seconds}s = ingestion {ingestion}s "
        f"+ reconciliation {reconciliation}s + healthcheck {healthcheck}s + "
        f"{gold_script_count} Gold scripts = {gold_total}s incl. overrides {gold_overrides}), with real margin "
        f"for setup/checkout/dependency-install/log-upload — not just barely more. "
        f"This exact scenario (job timeout == stage budget sum) cancelled a real "
        f"production run a few seconds before completion on 2026-09-22."
    )

    # Require actual margin, not just a technical ">" by one second —
    # setup/checkout/pip install/token provisioning/cleanup/log-upload
    # steps take real time too (observed ~15-20s combined in practice).
    margin_seconds = job_timeout_seconds - stage_budget_seconds
    assert margin_seconds >= 300, (
        f"job timeout exceeds the stage budget by only {margin_seconds}s — "
        f"that's too tight a margin for setup/checkout/install/cleanup/log-upload "
        f"overhead; want at least 300s of headroom"
    )


def test_p11_quinque_expected_stage_values():
    """P11-QUINQUE Q8 / P11-LAST-MILE — pins the exact values (reconciliation
    3600 -> 6000 and job timeout 200 -> 240 in P11-LAST-MILE), so a
    future edit that silently changes one of them (without also updating
    the reasoning in the workflow comment) fails loudly here first."""
    assert _int_constant(CYCLE_SRC, "INGESTION_TIMEOUT_SECONDS") == 3600
    assert _int_constant(CYCLE_SRC, "GOLD_SCRIPT_TIMEOUT_SECONDS") == 600
    assert _int_constant(CYCLE_SRC, "RECONCILIATION_TIMEOUT_SECONDS") == 6000
    assert _int_constant(CYCLE_SRC, "HEALTHCHECK_TIMEOUT_SECONDS") == 120
    assert _gold_chain_length(CYCLE_SRC) == 7
    assert _job_timeout_minutes(WORKFLOW_SRC) == 250
    assert _gold_overrides(CYCLE_SRC) == {"_p6a_gold_build.py": 1200}


def test_domain_worker_timeout_selection():
    """P11-QUINQUE Q5 — TIKTOK/orders gets a targeted 900s override
    (proven live to need more than the 600s default on a cold/catch-up
    window); every other non-finance domain, on either platform, stays
    at the 600s default; finance (both platforms) keeps its existing
    3600s allowance. Reads pipelines/incremental.py's own
    DEFAULT_WORKER_TIMEOUT_SECONDS/DOMAIN_TIMEOUT_SECONDS directly rather
    than re-deriving the lookup logic here, so this fails loudly if that
    dict's keys or the (source_system, domain) lookup shape ever change."""
    namespace: dict = {"os": __import__("os")}
    # Isolate exactly the constants block — this module also does
    # subprocess/DB work at import time we don't want to trigger here.
    m = re.search(
        r"DEFAULT_WORKER_TIMEOUT_SECONDS = .*?\nDOMAIN_TIMEOUT_SECONDS = \{.*?\n\}",
        INCREMENTAL_SRC, re.DOTALL,
    )
    assert m, "could not find the timeout constants block in pipelines/incremental.py"
    exec(m.group(0), namespace)  # noqa: S102 — trusted local source file, test-only

    default = namespace["DEFAULT_WORKER_TIMEOUT_SECONDS"]
    domain_timeouts = namespace["DOMAIN_TIMEOUT_SECONDS"]

    assert default == 600

    def resolve(source_system, domain):
        return domain_timeouts.get((source_system, domain), default)

    assert resolve("TIKTOK", "orders") == 900
    assert resolve("SHOPEE", "finance") == 3600
    assert resolve("TIKTOK", "finance") == 3600
    # unchanged / untouched, per Q5 — "Shopee orders and unrelated domains remain unchanged"
    assert resolve("SHOPEE", "orders") == 600
    assert resolve("SHOPEE", "returns") == 600
    assert resolve("TIKTOK", "product_analytics") == 600
