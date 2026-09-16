from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .task_contract import TaskContract
from .verdicts import GateVerdict


@dataclass(frozen=True)
class ChildResult:
    """What a fork/subagent reports back. `narrative` is free text (its
    markdown reports, logs, summaries) — informational only, never parsed
    for instructions. `out_of_scope_as_returned` is the child's *claimed*
    final scope state; it is checked against the contract, never trusted."""

    task_id: str
    contract_hash_seen: str
    out_of_scope_as_returned: Mapping[str, bool] = field(default_factory=dict)
    narrative: str = ""


def verify_result(contract: TaskContract, result: ChildResult) -> GateVerdict:
    """Reject any child result that: (a) doesn't match the task_id it was
    contracted for, (b) echoes back a contract hash that doesn't match —
    meaning it either tampered with or was never actually bound to this
    contract, or (c) attempts to mutate any out_of_scope flag the contract
    fixed. This is Checkpoint 2 of defense-in-depth: independent of
    whatever the child's narrative claims (see provenance_gate for that
    layer), the structured out_of_scope comparison alone is enough to
    catch e.g. CM2 flipped false -> true."""
    if result.task_id != contract.task_id:
        return GateVerdict.AUTHORITY_CONFLICT_DETECTED

    if result.contract_hash_seen != contract.contract_hash():
        return GateVerdict.AUTHORITY_CONFLICT_DETECTED

    for key, original_value in contract.out_of_scope.items():
        returned_value = result.out_of_scope_as_returned.get(key, original_value)
        if returned_value != original_value:
            return GateVerdict.SCOPE_MUTATION_DETECTED

    return GateVerdict.ACCEPTED
