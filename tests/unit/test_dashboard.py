from ai_sre.core.audit import AuditLog
from ai_sre.selfmonitor.dashboard import summarize
from tests.conftest import FakeAdapter


async def test_orchestrator_persists_each_website_poll(make_orchestrator):
    adapter = FakeAdapter(
        "website",
        {},
        {"targets": [{
            "resource_id": "portfolio",
            "name": "portfolio",
            "ok": True,
            "latency_ms": 125.4,
        }]},
        ok=True,
        config={"interval": 60},
    )
    orchestrator = make_orchestrator({"website": adapter})

    await orchestrator.poll_once(adapter)

    checks = orchestrator.audit.read("health_checks.jsonl")
    assert len(checks) == 1
    assert checks[0]["ok"] is True
    assert checks[0]["interval_seconds"] == 60.0


def test_dashboard_summarizes_website_availability_and_latency(tmp_path):
    audit = AuditLog(tmp_path)
    for ok, latency in [(True, 100), (True, 200), (False, 500), (True, 300)]:
        audit.health_check(
            adapter="website",
            ok=ok,
            interval_seconds=60,
            observed={"targets": [{
                "resource_id": "portfolio",
                "name": "portfolio",
                "ok": ok,
                "latency_ms": latency,
            }]},
        )

    target = summarize(audit)["website_targets"]["portfolio"]

    assert target == {
        "checks": 4,
        "successful_checks": 3,
        "availability_pct": 75.0,
        "average_latency_ms": 275.0,
        "p95_latency_ms": 500.0,
    }


def test_dashboard_handles_website_failures_without_latency(tmp_path):
    audit = AuditLog(tmp_path)
    audit.health_check(
        adapter="website",
        ok=False,
        error="collector raised RuntimeError",
        interval_seconds=60,
        observed={"targets": [{"resource_id": "portfolio", "ok": False}]},
    )

    target = summarize(audit)["website_targets"]["portfolio"]

    assert target["availability_pct"] == 0.0
    assert target["average_latency_ms"] is None
    assert target["p95_latency_ms"] is None
