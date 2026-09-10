"""Severity guardrails, approval queue mechanics, and report rendering."""

from ai_sre.core.approvals import ApprovalQueue, apply_severity_guardrails
from ai_sre.core.models import (
    AnomalyEvent,
    Incident,
    RemediationAction,
    RemediationResult,
    Severity,
)
from ai_sre.core.report import render_report


def _action(adapter="stripe", action="replay_webhook", severity=Severity.P3):
    return RemediationAction(anomaly_id="a1", adapter=adapter, action=action, severity=severity)


def test_guardrail_raises_stripe_floor_to_p2():
    decided = apply_severity_guardrails(_action(severity=Severity.P3))
    assert decided.severity == Severity.P2


def test_guardrail_never_lowers_severity():
    decided = apply_severity_guardrails(_action(severity=Severity.P1))
    assert decided.severity == Severity.P1


def test_guardrail_rollback_is_p1():
    decided = apply_severity_guardrails(_action(adapter="deploy", action="rollback",
                                                severity=Severity.P2))
    assert decided.severity == Severity.P1


def test_approval_queue_roundtrip(tmp_path):
    queue = ApprovalQueue(tmp_path)
    anomaly = AnomalyEvent(adapter="deploy", anomaly_type="bad_deploy",
                           severity_hint=Severity.P1)
    incident = Incident(anomaly=anomaly, action=_action(adapter="deploy", action="rollback",
                                                        severity=Severity.P1),
                        status="awaiting_approval")
    queue.enqueue(incident)
    assert [i.id for i in queue.pending()] == [incident.id]

    resolved = queue.resolve(incident.id, approved=True, by="abhay")
    assert resolved.approved_by == "abhay"
    assert queue.pending() == []
    assert queue.resolve("nonexistent", approved=True) is None


def test_report_is_owner_friendly():
    anomaly = AnomalyEvent(adapter="stripe", anomaly_type="stripe_order_mismatch",
                           severity_hint=Severity.P2, context={"charge_id": "ch_1"})
    incident = Incident(
        anomaly=anomaly,
        action=RemediationAction(anomaly_id=anomaly.id, adapter="stripe",
                                 action="replay_webhook", severity=Severity.P2,
                                 rationale="A payment came in but the order never reached the kitchen."),
        result=RemediationResult(ok=True, detail="webhook replay returned 200"),
        status="remediated",
    )
    report = render_report(incident)
    assert "charged but their order didn't come through" in report
    assert "replay_webhook" in report
    assert "and it succeeded" in report
    assert "```json" in report  # technical appendix for the developer


def test_report_awaiting_approval_includes_cli_hint():
    anomaly = AnomalyEvent(adapter="deploy", anomaly_type="bad_deploy",
                           severity_hint=Severity.P1)
    incident = Incident(anomaly=anomaly,
                        action=_action(adapter="deploy", action="rollback", severity=Severity.P1),
                        status="awaiting_approval")
    report = render_report(incident)
    assert f"ai-sre approve {incident.id}" in report
