from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from .instruction_source import TRUSTED_SOURCES, InstructionSource
from .verdicts import GateVerdict

# Phrases observed in the real incident (task a1a55326e796526bd), where a
# forked subagent's own generated narrative implied it had received an
# instruction it never actually received ("PHASE 18 REVISED", "CRITICAL
# RECOVERY", claims the user/coordinator "approved" or "told me" something,
# and self-declared production-readiness). Matching one of these NEVER
# grants authority by itself — it only marks the text for logging so a
# human/parent can see the attempt. See scan_for_self_asserted_authority.
SELF_ASSERTED_AUTHORITY_PATTERNS = [
    re.compile(r"phase\s+\d+[a-z]?\s*(revised|start|b\b)", re.IGNORECASE),
    re.compile(r"critical\s+recovery", re.IGNORECASE),
    re.compile(r"user\s+told\s+me", re.IGNORECASE),
    re.compile(r"\b(user|coordinator)\s+(approved|confirmed|authorized|instructed)\b", re.IGNORECASE),
    re.compile(r"production\s+may\s+proceed", re.IGNORECASE),
    re.compile(r"can\s+proceed\s+to\s+production", re.IGNORECASE),
    re.compile(r"ready\s+for\s+production", re.IGNORECASE),
]


@dataclass(frozen=True)
class ScopeInstruction:
    """A candidate instruction that claims the right to change task scope."""

    source: InstructionSource
    source_id: Optional[str]  # must match a registry entry to be trusted
    text: str


class ProvenanceGate:
    """Checkpoint: nothing may change a TaskContract's scope unless it is
    traceably SYSTEM, USER, or PARENT_TASK in origin AND its source_id is
    independently registered — never merely self-claimed. This is the
    direct fix for the root failure: a subagent asserting a fabricated
    "PHASE 18 REVISED" / "CRITICAL RECOVERY" instruction with no real
    source_id must be rejected, not accepted at face value.
    """

    def __init__(self, trusted_source_ids: Optional[Iterable[str]] = None) -> None:
        self._trusted_source_ids = set(trusted_source_ids or ())

    def register_trusted_source(self, source_id: str) -> None:
        if not source_id or not source_id.strip():
            raise ValueError("cannot register an empty source_id as trusted")
        self._trusted_source_ids.add(source_id)

    def is_registered(self, source_id: Optional[str]) -> bool:
        return bool(source_id) and source_id in self._trusted_source_ids

    def authorize(self, instruction: ScopeInstruction) -> GateVerdict:
        if instruction.source not in TRUSTED_SOURCES:
            return GateVerdict.UNTRUSTED_INSTRUCTION_SOURCE
        if not self.is_registered(instruction.source_id):
            return GateVerdict.UNTRUSTED_INSTRUCTION_SOURCE
        return GateVerdict.ACCEPTED

    def resolve(
        self, instructions: List[ScopeInstruction]
    ) -> Tuple[GateVerdict, Optional[ScopeInstruction]]:
        """Resolve a set of candidate instructions to at most one accepted
        one. If none are authorized, return the rejection. If more than one
        is independently authorized but they disagree, STOP: return
        AUTHORITY_CONFLICT_DETECTED rather than silently picking one."""
        if not instructions:
            return GateVerdict.UNTRUSTED_INSTRUCTION_SOURCE, None

        verdicts = [(instr, self.authorize(instr)) for instr in instructions]
        accepted = [instr for instr, v in verdicts if v == GateVerdict.ACCEPTED]

        if not accepted:
            return verdicts[0][1], None

        if len(accepted) > 1 and len({i.text for i in accepted}) > 1:
            return GateVerdict.AUTHORITY_CONFLICT_DETECTED, None

        return GateVerdict.ACCEPTED, accepted[0]

    @staticmethod
    def scan_for_self_asserted_authority(text: str) -> List[str]:
        """Detect phrases a child task might use to imply it received an
        instruction it did not. Returns matched pattern strings for
        logging/alerting ONLY — callers must never use this to authorize
        anything (see governance.orchestrator.accept_child_result, which
        never derives scope from this)."""
        if not text:
            return []
        return [p.pattern for p in SELF_ASSERTED_AUTHORITY_PATTERNS if p.search(text)]
