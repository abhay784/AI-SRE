"""Golden-path regression suite (PRD §9.6).

Each test drives one failure mode through the whole pipeline —
poll -> rules -> reason -> gate -> remediate -> report -> audit — using fake
adapters and the deterministic FakeReasoner, so the suite runs anywhere.
Live-cluster and live-LLM variants layer on top of these (see README).
"""

from pathlib import Path

from ai_sre.core.models import Severity
from tests.conftest import FakeAdapter

STRIPE_ACTIONS = {"replay_webhook": "replay", "reconstruct_order": "rebuild"}
PG_ACTIONS = {"kill_idle_connections": "kill idle"}
S3_ACTIONS = {"revert_policy": "revert"}
TRAFFIC_ACTIONS = {"block_ip": "block", "enable_captcha": "captcha"}
DEPLOY_ACTIONS = {"rollback": "roll back"}


async def test_stripe_mismatch_autonomous_p2(make_orchestrator, tmp_path):
    adapter = FakeAdapter("stripe", STRIPE_ACTIONS, {
        "unmatched_charges": [{"id": "ch_42", "amount": 4200}],
        "webhook_error_count": 0,
    })
    orch = make_orchestrator({"stripe": adapter})
    incidents = await orch.poll_once(adapter)

    assert len(incidents) == 1
    incident = incidents[0]
    # guardrail keeps stripe at >= P2, executed autonomously with notification
    assert incident.action.severity == Severity.P2
    assert incident.status == "remediated"
    assert adapter.remediations == [("replay_webhook", {"charge_id": "ch_42"})]
    assert (Path(tmp_path) / "incidents" / f"{incident.id}.md").exists()
    assert orch.audit.read("actions.jsonl")[0]["anomaly_type"] == "stripe_order_mismatch"


async def test_bot_abuse_block_ip_carries_the_ip_param(make_orchestrator):
    """FakeReasoner is also the LLM-outage fallback (see reasoning.py), so a
    proposed block_ip must carry the actual IP from anomaly context — not an
    empty params dict the adapter will reject. Found live: the sandbox's bot
    traffic chaos script triggered endpoint_abuse but block_ip failed with
    'requires an ip param' because params weren't threaded through."""
    adapter = FakeAdapter("traffic", TRAFFIC_ACTIONS, {"top_ips": {"203.0.113.66": 15.0}})
    orch = make_orchestrator({"traffic": adapter})
    (incident,) = await orch.poll_once(adapter)
    assert incident.status == "remediated"
    assert adapter.remediations == [("block_ip", {"ip": "203.0.113.66"})]


async def test_db_pool_p2_kills_idle_connections(make_orchestrator):
    adapter = FakeAdapter("postgres", PG_ACTIONS,
                          {"active_connections": 17, "max_connections": 20})
    orch = make_orchestrator({"postgres": adapter})
    (incident,) = await orch.poll_once(adapter)
    assert incident.status == "remediated"
    assert adapter.remediations[0][0] == "kill_idle_connections"


async def test_public_bucket_is_p1_and_waits_for_approval(make_orchestrator):
    adapter = FakeAdapter("s3", S3_ACTIONS, {"policy_drifted": True, "public_access": True})
    orch = make_orchestrator({"s3": adapter})
    (incident,) = await orch.poll_once(adapter)

    # P1: nothing executed until a human approves
    assert incident.status == "awaiting_approval"
    assert adapter.remediations == []
    assert [i.id for i in orch.approvals.pending()] == [incident.id]

    approved = await orch.execute_approved(incident.id, by="test-human")
    assert approved.status == "remediated"
    assert approved.approved_by == "test-human"
    assert adapter.remediations == [("revert_policy", {})]


async def test_failed_p1_execution_is_requeued_not_dropped(make_orchestrator):
    """A P1 revert that fails after approval (bad credentials, network blip)
    must stay visible and retryable — not vanish from the approval queue.
    Found live: a failed `ai-sre approve` on a public-bucket incident
    silently removed it from `pending` with the bucket still exposed."""
    adapter = FakeAdapter("s3", S3_ACTIONS, {"policy_drifted": True, "public_access": True})
    adapter.fail_remediation = True
    orch = make_orchestrator({"s3": adapter})
    (incident,) = await orch.poll_once(adapter)
    assert incident.status == "awaiting_approval"

    failed = await orch.execute_approved(incident.id, by="test-human")
    assert failed.status == "awaiting_approval"
    assert failed.approved_by is None  # failed run is not "approved and done"
    assert failed.result.ok is False

    # still visible for a retry, not silently dropped
    assert [i.id for i in orch.approvals.pending()] == [incident.id]

    # now succeeds
    adapter.fail_remediation = False
    retried = await orch.execute_approved(incident.id, by="test-human")
    assert retried.status == "remediated"
    assert orch.approvals.pending() == []


async def test_bad_deploy_rollback_requires_approval(make_orchestrator):
    from datetime import datetime, timezone

    adapter = FakeAdapter("deploy", DEPLOY_ACTIONS, {
        "error_rate": 0.4,
        "last_deploy_at": datetime.now(timezone.utc).isoformat(),
        "deployment": "api-gateway",
    })
    orch = make_orchestrator({"deploy": adapter})
    (incident,) = await orch.poll_once(adapter)
    assert incident.action.action == "rollback"
    assert incident.action.severity == Severity.P1
    assert incident.status == "awaiting_approval"
    assert adapter.remediations == []


async def test_form_spam_p3_fully_autonomous(make_orchestrator):
    adapter = FakeAdapter("traffic", TRAFFIC_ACTIONS, {"form_submissions_per_min": 40})
    orch = make_orchestrator({"traffic": adapter})
    (incident,) = await orch.poll_once(adapter)
    assert incident.action.severity == Severity.P3
    assert incident.status == "remediated"
    assert adapter.remediations == [("enable_captcha", {})]


async def test_backup_failure_escalates(make_orchestrator):
    adapter = FakeAdapter("backup", {}, {"backup_age_hours": 50, "test_restore_ok": False})
    orch = make_orchestrator({"backup": adapter})
    (incident,) = await orch.poll_once(adapter)
    assert incident.action.action == "escalate"
    assert incident.status == "escalated"
    assert adapter.remediations == []


async def test_alert_only_mode_never_remediates(make_orchestrator):
    adapter = FakeAdapter("traffic", TRAFFIC_ACTIONS, {"form_submissions_per_min": 40})
    orch = make_orchestrator({"traffic": adapter}, alert_only=True)
    (incident,) = await orch.poll_once(adapter)
    assert incident.status == "escalated"
    assert adapter.remediations == []


async def test_failed_remediation_is_reported_honestly(make_orchestrator):
    adapter = FakeAdapter("postgres", PG_ACTIONS,
                          {"active_connections": 17, "max_connections": 20})
    adapter.fail_remediation = True
    orch = make_orchestrator({"postgres": adapter})
    (incident,) = await orch.poll_once(adapter)
    assert incident.status == "failed"
    assert orch.audit.read("actions.jsonl")[0]["status"] == "failed"


async def test_quiet_signals_never_wake_the_pipeline(make_orchestrator):
    adapter = FakeAdapter("postgres", PG_ACTIONS,
                          {"active_connections": 3, "max_connections": 20}, ok=True)
    orch = make_orchestrator({"postgres": adapter})
    assert await orch.poll_once(adapter) == []
    assert orch.audit.read("actions.jsonl") == []
