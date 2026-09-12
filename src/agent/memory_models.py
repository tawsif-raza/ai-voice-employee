"""
Typed durable-memory records (Phase 5; plan.md Step 5.7).

Deliberately narrow taxonomy — PREFERENCE, WORKFLOW_CONTEXT,
COMMUNICATION_PREFERENCE only, per plan.md's explicit instruction: "Do
NOT create broad categories for sensitive medical information... Avoid
turning memory into an unrestricted patient profile." Nothing in this
module or memory_manager.py stores clinical/health content by category
design; configs/policies/privacy.yaml's existing `persist`-restricted
field list (medical_condition, payment_method) is the actual enforcement
point (see memory_manager.py's use of PolicyEngine.evaluate_privacy()).
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class MemoryCategory(str, Enum):
    PREFERENCE = "PREFERENCE"
    WORKFLOW_CONTEXT = "WORKFLOW_CONTEXT"
    COMMUNICATION_PREFERENCE = "COMMUNICATION_PREFERENCE"


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    user_id: str
    category: MemoryCategory
    key: str
    value: str
    source: str  # e.g. "user_explicit", "conversation_manager" -- never "llm_output" (see memory_manager.py)
    created_at: datetime
    updated_at: datetime
    expires_at: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)
    version: int = 1

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(timezone.utc)) >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "category": self.category.value,
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "metadata": dict(self.metadata),
            "version": self.version,
        }


def new_memory_id() -> str:
    return f"mem_{uuid.uuid4().hex[:16]}"
