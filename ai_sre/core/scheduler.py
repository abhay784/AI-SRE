"""Orchestrator: poll -> rules -> reason -> gate -> remediate -> report.

Each adapter polls on its own cadence (PRD §7). The LLM only runs when the
deterministic rule layer emits an AnomalyEvent.
"""

from __future__ import annotations

import asyncio
import logging

from .adapter import BaseAdapter
from .approvals import ApprovalQueue, apply_severity_guardrails
from .audit import AuditLog
from .models import HealthResult, Incident, RemediationResult, Severity
from .reasoning import FakeReasoner, Reasoner
from .notifications import WebhookNotifier
from .report import write_report
from .rules import RuleEngine

log = logging.getLogger("ai_sre")


class Orchestrator:
    def __init__(
        self,
        adapters: dict[str, BaseAdapter],
        reasoner: Reasoner,
        *,
        rules: RuleEngine | None = None,
        audit: AuditLog | None = None,
        approvals: ApprovalQueue | None = None,
        alert_only: bool = False,
        var_root: str = "var",
        notifier: WebhookNotifier | None = None,
    ):
        self.adapters = adapters
        self.reasoner = reasoner
        self.rules = rules or RuleEngine()
        self.audit = audit or AuditLog(f"{var_root}/audit")
        self.approvals = approvals or ApprovalQueue(f"{var_root}/approvals")
        self.alert_only = alert_only
        self.incident_root = f"{var_root}/incidents"
        self.incidents: list[Incident] = []
        self.notifier = notifier

    # -- polling loops --------------------------------------------------------

    async def run(self) -> None:
        """Run one polling task per adapter, forever."""
        tasks = [asyncio.create_task(self._poll_loop(a), name=f"poll:{name}")
                 for name, a in self.adapters.items()]
        await asyncio.gather(*tasks)

    async def _poll_loop(self, adapter: BaseAdapter) -> None:
        interval = float(adapter.config.get("interval", adapter.default_interval))
        while True:
            try:
                await self.poll_once(adapter)
            except Exception:
                # A broken integration/report must not kill every monitor.
                log.error("poll pipeline failed for %s; retrying on next interval", adapter.name)
            await asyncio.sleep(interval)

    async def poll_once(self, adapter: BaseAdapter) -> list[Incident]:
        """One poll of one adapter, through the whole pipeline. Returns any
        incidents created — this is also the entry point the tests drive."""
        try:
            health = await adapter.check_health()
        except Exception as exc:
            health = HealthResult(adapter=adapter.name, ok=False,
                                  error=f"collector raised {type(exc).__name__}")
        incidents = []
        for anomaly in self.rules.evaluate(health):
            log.warning("anomaly detected: %s/%s", anomaly.adapter, anomaly.anomaly_type)
            incidents.append(await self.handle_anomaly(anomaly))
        return incidents

    # -- anomaly pipeline -------------------------------------------------------

    async def get_adapter_context(self, adapter_name: str) -> dict:
        adapter = self.adapters.get(adapter_name)
        if adapter is None:
            return {"error": f"no adapter named {adapter_name!r}"}
        return await adapter.get_context()

    async def handle_anomaly(self, anomaly) -> Incident:
        adapter = self.adapters[anomaly.adapter]
        reasoner = FakeReasoner() if anomaly.anomaly_type == "monitor_recovered" else self.reasoner
        decision = await reasoner.decide(anomaly, adapter.ACTIONS)
        # Enforce the adapter boundary even for injected/custom reasoners.
        decision.adapter = anomaly.adapter
        decision.anomaly_id = anomaly.id
        if decision.action not in adapter.ACTIONS and decision.action not in {"none", "escalate"}:
            decision.action = "escalate"
        decision = apply_severity_guardrails(decision)
        incident = Incident(anomaly=anomaly, action=decision)

        if anomaly.anomaly_type == "monitor_recovered":
            incident.status = "recovered"
        elif decision.action == "none":
            incident.status = "closed"
        elif decision.action == "escalate" or self.alert_only:
            incident.status = "escalated"
        elif decision.severity == Severity.P1:
            incident.status = "awaiting_approval"
            self.approvals.enqueue(incident)
        else:
            incident.result = await adapter.remediate(decision.action, decision.params)
            incident.status = "remediated" if incident.result.ok else "failed"

        self._record(incident)
        await self._notify(incident)
        return incident

    async def execute_approved(self, incident_id: str, *, by: str = "cli") -> Incident | None:
        """Run a P1 remediation after human approval (called by the CLI).

        If execution fails (bad credentials, transient network error, ...),
        the incident is re-queued as awaiting approval rather than silently
        dropped — a failed revert of a public bucket, for example, must stay
        visible and retryable, not vanish because one attempt errored.
        """
        incident = self.approvals.resolve(incident_id, approved=True, by=by)
        if incident is None or incident.action is None:
            return None
        adapter = self.adapters.get(incident.action.adapter)
        if adapter is None:
            incident.result = RemediationResult(ok=False, detail="adapter no longer configured")
        else:
            incident.result = await adapter.remediate(incident.action.action, incident.action.params)
        incident.status = "remediated" if incident.result.ok else "failed"
        if not incident.result.ok:
            incident.status = "awaiting_approval"
            incident.approved_by = None
            self.approvals.enqueue(incident)
        self._record(incident)
        await self._notify(incident)
        return incident

    async def _notify(self, incident: Incident) -> None:
        if self.notifier and incident.status != "closed":
            delivered = await self.notifier.send(incident)
            self.audit._append("notifications.jsonl", {"incident_id": incident.id,
                                                       "delivered": delivered})

    def _record(self, incident: Incident) -> None:
        self.incidents.append(incident)
        del self.incidents[:-1000]
        action = incident.action
        self.audit.action(
            incident_id=incident.id,
            anomaly_type=incident.anomaly.anomaly_type,
            adapter=incident.anomaly.adapter,
            action=action.action if action else "-",
            params=action.params if action else {},
            severity=(action.severity.value if action else incident.anomaly.severity_hint.value),
            status=incident.status,
            detail=incident.result.detail if incident.result else "",
            approved_by=incident.approved_by,
        )
        if incident.status != "closed":
            path = write_report(incident, self.incident_root)
            log.info("incident report written: %s", path)
