"""Core data models shared by collectors, rules, reasoning, and remediation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class Severity(str, Enum):
    """PRD severity tiers. P1 requires human approval before remediation."""

    P1 = "P1"  # high impact / user-facing / financial — propose and wait
    P2 = "P2"  # moderate — autonomous remediation + immediate notification
    P3 = "P3"  # low — fully autonomous remediation


class HealthResult(BaseModel):
    """One poll of one adapter. `observed` is the raw signal payload."""

    adapter: str
    ok: bool
    observed: dict = Field(default_factory=dict)
    checked_at: datetime = Field(default_factory=_now)
    error: str | None = None  # collector itself failed (network, auth, ...)


class AnomalyEvent(BaseModel):
    """Produced by the deterministic rule layer — the only thing that wakes the LLM."""

    id: str = Field(default_factory=_new_id)
    adapter: str
    anomaly_type: str
    severity_hint: Severity
    context: dict = Field(default_factory=dict)
    detected_at: datetime = Field(default_factory=_now)

    def context_packet(self) -> dict:
        """The JSON packet handed to the reasoning agent (PRD §7)."""
        return {
            "timestamp": self.detected_at.isoformat(),
            "anomaly_type": self.anomaly_type,
            "adapter": self.adapter,
            "severity_hint": self.severity_hint.value,
            **self.context,
        }


class RemediationAction(BaseModel):
    """A decision made by the reasoning layer (or a deterministic default)."""

    anomaly_id: str
    adapter: str
    action: str  # must be a key in the adapter's ACTIONS registry, or "escalate"/"none"
    params: dict = Field(default_factory=dict)
    severity: Severity
    rationale: str = ""


class RemediationResult(BaseModel):
    ok: bool
    detail: str = ""
    executed_at: datetime = Field(default_factory=_now)


class Incident(BaseModel):
    """The full lifecycle record used to render the owner-facing report."""

    id: str = Field(default_factory=_new_id)
    anomaly: AnomalyEvent
    action: RemediationAction | None = None
    result: RemediationResult | None = None
    approved_by: str | None = None  # set on P1 approvals
    status: str = "open"  # open | remediated | escalated | awaiting_approval | failed
