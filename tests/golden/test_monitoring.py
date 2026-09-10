import httpx

from slopsaver.adapters.website_adapter import WebsiteAdapter
from slopsaver.core.models import HealthResult, RemediationAction, Severity
from slopsaver.core.rules import RuleEngine
from tests.conftest import FakeAdapter


async def test_website_failure_confirmation_dedupe_recovery_and_relapse(make_orchestrator, tmp_path):
    status = 503
    adapter = WebsiteAdapter({"targets": [{"name": n, "url": f"https://{n}.test"} for n in ["one", "two"]]},
                             transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    orch = make_orchestrator({"website": adapter})
    assert await orch.poll_once(adapter) == []
    failures = await orch.poll_once(adapter)
    assert len(failures) == 2  # One target's incident must not suppress another's.
    assert all(i.status == "escalated" for i in failures)
    assert await orch.poll_once(adapter) == []
    status = 200
    recoveries = await orch.poll_once(adapter)
    assert len(recoveries) == 2
    assert all(i.status == "recovered" for i in recoveries)
    assert await orch.poll_once(adapter) == []
    status = 503
    assert await orch.poll_once(adapter) == []
    assert len(await orch.poll_once(adapter)) == 2
    assert len(list((tmp_path / "incidents").glob("*.md"))) == 6


def test_transient_website_failure_resets_streak():
    rules = RuleEngine()
    def sample(ok):
        return rules.evaluate(HealthResult(adapter="website", ok=ok,
                              observed={"targets": [{"resource_id": "site", "ok": ok}]}))
    assert sample(False) == []
    assert sample(True) == []
    assert sample(False) == []
    assert len(sample(False)) == 1


def test_github_failures_are_independent_and_api_outage_does_not_resolve_workflow():
    rules = RuleEngine()
    def poll(error=None, ok=False):
        repo = {"resource_id": "owner/repo@main", "error": error, "runs": [] if error else [
            {"resource_id": f"owner/repo@main:{w}", "ok": ok} for w in [1, 2]]}
        return rules.evaluate(HealthResult(adapter="github", ok=ok, observed={"repositories": [repo]}))
    assert len(poll()) == 2
    assert poll() == []
    assert [a.anomaly_type for a in poll(error="GitHub HTTP 403")] == ["collector_failure"]
    # Collection recovers, but workflow failures remain active and deduped.
    assert [a.anomaly_type for a in poll()] == ["monitor_recovered"]
    assert len(poll(ok=True)) == 2


async def test_raised_collector_failure_is_escalated(make_orchestrator):
    adapter = FakeAdapter("website", {}, {})
    async def broken():
        raise RuntimeError("secret")
    adapter.check_health = broken
    orch = make_orchestrator({"website": adapter})
    incident, = await orch.poll_once(adapter)
    assert incident.status == "escalated"
    assert "secret" not in incident.model_dump_json()


async def test_orchestrator_enforces_empty_allowlist_even_with_custom_reasoner(make_orchestrator):
    adapter = FakeAdapter("website", {}, {"targets": [{"resource_id": "site", "ok": False,
                                                       "failure_threshold": 1}]})
    orch = make_orchestrator({"website": adapter})
    class UntrustedReasoner:
        async def decide(self, anomaly, actions):
            return RemediationAction(anomaly_id="wrong", adapter="deploy", action="rollback",
                                     severity=Severity.P3)
    orch.reasoner = UntrustedReasoner()
    incident, = await orch.poll_once(adapter)
    assert incident.action.adapter == "website"
    assert incident.action.action == "escalate"
    assert adapter.remediations == []
