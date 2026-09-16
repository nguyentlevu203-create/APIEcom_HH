from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .provenance_gate import ProvenanceGate
from .result_validator import ChildResult, verify_result
from .task_contract import TaskContract
from .verdicts import GateVerdict


@dataclass(frozen=True)
class ProcessedResult:
    verdict: GateVerdict
    suspicious_phrases_detected: List[str]
    out_of_scope_final: Dict[str, bool]


def accept_child_result(contract: TaskContract, result: ChildResult) -> ProcessedResult:
    """The ONE place code in this project may act on a fork/subagent's
    output. Two independent guarantees, both required:

    1. `out_of_scope_final` is always `dict(contract.out_of_scope)` — taken
       from the immutable contract the parent itself created, never from
       `result.out_of_scope_as_returned` and never derived from
       `result.narrative`. Even when verify_result() returns ACCEPTED, the
       child's claimed scope is discarded in favor of the contract's own.
       A child can confirm the contract; it can never expand it.
    2. `verdict` still surfaces SCOPE_MUTATION_DETECTED / AUTHORITY_CONFLICT
       so the parent can log/alert/refuse to use the result at all — the
       guarantee in (1) is defense in depth, not a reason to skip this.

    Narrative is scanned (see ProvenanceGate.scan_for_self_asserted_authority)
    purely so a human/parent can see a child tried to assert authority
    (e.g. "PHASE 18 REVISED", "CRITICAL RECOVERY", "ready for production")
    — the scan result is never fed back into scope or phase state.
    """
    suspicious = ProvenanceGate.scan_for_self_asserted_authority(result.narrative)
    verdict = verify_result(contract, result)
    return ProcessedResult(
        verdict=verdict,
        suspicious_phrases_detected=suspicious,
        out_of_scope_final=dict(contract.out_of_scope),
    )
