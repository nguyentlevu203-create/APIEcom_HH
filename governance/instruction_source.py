from __future__ import annotations

from enum import Enum


class InstructionSource(str, Enum):
    """Where a candidate scope-changing instruction claims to originate.

    Only SYSTEM, USER, and PARENT_TASK are ever trustable — and even then
    only when paired with a source_id that is independently registered
    (see ProvenanceGate), never merely self-asserted.
    """

    SYSTEM = "SYSTEM"
    USER = "USER"
    PARENT_TASK = "PARENT_TASK"
    SUBAGENT_GENERATED = "SUBAGENT_GENERATED"  # narrative, logs, artifacts, summaries — NEVER authoritative
    UNKNOWN = "UNKNOWN"


TRUSTED_SOURCES = frozenset(
    {InstructionSource.SYSTEM, InstructionSource.USER, InstructionSource.PARENT_TASK}
)
