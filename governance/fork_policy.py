from __future__ import annotations

from typing import Optional

from .instruction_source import InstructionSource

# Automatic resume of a previously-dispatched fork/subagent task is
# disabled by design. The real incident happened, in part, because a
# completed fork was resumed and then produced a second round of
# fabricated authority. Resuming must always require a fresh, explicit,
# traceable authorization — never happen implicitly (e.g. because a task
# is still "pending" or because resuming is convenient).
AUTO_RESUME_ENABLED = False


def may_resume(source: InstructionSource, source_id: Optional[str], trusted_source_ids) -> bool:
    """Whether resuming a fork/subagent task is permitted right now.

    Even if AUTO_RESUME_ENABLED were ever flipped true by mistake, this
    function still requires the resume request to come from SYSTEM or USER
    with a source_id present in the caller's trusted registry — there is
    no code path where AUTO_RESUME_ENABLED alone grants a resume.
    """
    if source not in (InstructionSource.SYSTEM, InstructionSource.USER):
        return False
    if not source_id or source_id not in trusted_source_ids:
        return False
    return True
