"""Severity gating + human approval flow (PRD §7).

P3 -> execute silently. P2 -> execute + notify. P1 -> queue the proposal and
wait for a human (CLI: `slopsaver approve <incident_id>`).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import Incident, RemediationAction, Severity


def apply_severity_guardrails(action: RemediationAction) -> RemediationAction:
    """Deterministic floor/ceiling on the LLM's severity classification.

    The reasoning agent picks a severity, but policy — not the model — has the
    final word on what runs unattended. This function may only *raise*
    severity, never lower it below the model's choice.

    TODO(user): this is the core trust policy of the product and worth shaping
    yourself. The default below enforces two floors:
      - anything touching payments (stripe adapter) is at least P2
      - destructive-sounding actions (rollback, kill, revert of a public
        bucket) are at least P1 until the action has earned autonomy
    Consider: per-customer overrides? an "earned autonomy" counter that lowers
    the floor after N successful supervised runs?
    """
    order = {Severity.P3: 0, Severity.P2: 1, Severity.P1: 2}

    def at_least(sev: Severity) -> None:
        if order[sev] > order[action.severity]:
            action.severity = sev

    if action.adapter == "stripe" and action.action not in ("none",):
        at_least(Severity.P2)
    if action.action in ("rollback",):
        at_least(Severity.P1)
    return action


class ApprovalQueue:
    """File-backed queue of P1 proposals awaiting a human decision."""

    def __init__(self, root: str | Path = "var/approvals"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, incident_id: str) -> Path:
        return self.root / f"{incident_id}.json"

    def enqueue(self, incident: Incident) -> None:
        self._path(incident.id).write_text(incident.model_dump_json(indent=2))

    def pending(self) -> list[Incident]:
        return sorted(
            (Incident.model_validate_json(p.read_text()) for p in self.root.glob("*.json")),
            key=lambda i: i.anomaly.detected_at,
        )

    def resolve(self, incident_id: str, *, approved: bool, by: str = "cli") -> Incident | None:
        path = self._path(incident_id)
        if not path.exists():
            return None
        incident = Incident.model_validate_json(path.read_text())
        incident.approved_by = by if approved else None
        incident.status = "approved" if approved else "rejected"
        resolved_dir = self.root / "resolved"
        resolved_dir.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        (resolved_dir / f"{incident_id}.{incident.status}.{stamp}.json").write_text(
            incident.model_dump_json(indent=2)
        )
        path.unlink()
        return incident
