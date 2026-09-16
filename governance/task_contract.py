from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional


@dataclass(frozen=True)
class TaskContract:
    """An immutable contract issued to a single fork/subagent task.

    Immutability is enforced two ways: the dataclass itself is frozen
    (reassigning any attribute raises), and `scope`/`out_of_scope` are
    wrapped in MappingProxyType so the nested dicts can't be mutated in
    place either (a frozen dataclass alone would NOT stop that).

    `contract_hash()` is deterministic over (task_id, parent_task_id,
    source_id, source, scope, out_of_scope) — NOT over created_at, so the
    hash a parent computes before dispatch is exactly what it must see
    echoed back, unchanged, when it accepts a result.
    """

    task_id: str
    parent_task_id: Optional[str]
    source_id: str
    source: str  # an InstructionSource value
    scope: Mapping[str, bool]
    out_of_scope: Mapping[str, bool]
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("TaskContract requires a non-empty task_id")
        if not self.source_id or not self.source_id.strip():
            raise ValueError("TaskContract requires a traceable, non-empty source_id")
        object.__setattr__(self, "scope", MappingProxyType(dict(self.scope)))
        object.__setattr__(self, "out_of_scope", MappingProxyType(dict(self.out_of_scope)))

    def canonical_json(self) -> str:
        payload = {
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "source_id": self.source_id,
            "source": self.source,
            "scope": dict(sorted(self.scope.items())),
            "out_of_scope": dict(sorted(self.out_of_scope.items())),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def contract_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
