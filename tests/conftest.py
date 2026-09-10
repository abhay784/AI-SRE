"""Shared test fixtures: a fake adapter and an orchestrator factory that
keeps all state (audit, approvals, reports) inside tmp_path."""

from __future__ import annotations

import pytest

from ai_sre.core.adapter import BaseAdapter
from ai_sre.core.approvals import ApprovalQueue
from ai_sre.core.audit import AuditLog
from ai_sre.core.models import HealthResult, RemediationResult
from ai_sre.core.reasoning import FakeReasoner
from ai_sre.core.rules import RuleEngine
from ai_sre.core.scheduler import Orchestrator


class FakeAdapter(BaseAdapter):
    """Test double: canned health observations + a record of remediations."""

    def __init__(self, name: str, actions: dict[str, str], observed: dict,
                 ok: bool = False, config: dict | None = None):
        super().__init__(config or {})
        self.name = name
        self.ACTIONS = actions
        self._observed = observed
        self._ok = ok
        self.remediations: list[tuple[str, dict]] = []
        self.fail_remediation = False

    async def check_health(self) -> HealthResult:
        return HealthResult(adapter=self.name, ok=self._ok, observed=self._observed)

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        if action not in self.ACTIONS:
            return self._unknown_action(action)
        self.remediations.append((action, params))
        if self.fail_remediation:
            return RemediationResult(ok=False, detail="simulated failure")
        return RemediationResult(ok=True, detail=f"{action} executed")


@pytest.fixture
def make_orchestrator(tmp_path):
    def _make(adapters: dict[str, FakeAdapter], *, alert_only: bool = False) -> Orchestrator:
        return Orchestrator(
            adapters,
            FakeReasoner(),
            rules=RuleEngine(),
            audit=AuditLog(tmp_path / "audit"),
            approvals=ApprovalQueue(tmp_path / "approvals"),
            alert_only=alert_only,
            var_root=str(tmp_path),
        )

    return _make
