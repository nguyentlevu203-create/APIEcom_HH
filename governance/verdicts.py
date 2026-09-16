from __future__ import annotations

from enum import Enum


class GateVerdict(str, Enum):
    """Outcomes the provenance gate / result validator can return. Every
    caller must handle every member explicitly — there is no silent
    default-to-accept path."""

    ACCEPTED = "ACCEPTED"
    AUTHORITY_CONFLICT_DETECTED = "AUTHORITY_CONFLICT_DETECTED"
    UNTRUSTED_INSTRUCTION_SOURCE = "UNTRUSTED_INSTRUCTION_SOURCE"
    SCOPE_MUTATION_DETECTED = "SCOPE_MUTATION_DETECTED"
    INDEPENDENTLY_REPRODUCED = "INDEPENDENTLY_REPRODUCED"
