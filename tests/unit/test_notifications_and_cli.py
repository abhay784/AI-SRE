import json

import httpx
import pytest

from ai_sre.__main__ import _build_orchestrator, _check, main
from ai_sre.core.models import AnomalyEvent, Incident, RemediationAction, Severity
from ai_sre.core.notifications import WebhookNotifier
from ai_sre.core.registry import build_adapters
from ai_sre.core.report import render_report


def incident(status="escalated"):
    anomaly = AnomalyEvent(adapter="website", anomaly_type="website_unhealthy", severity_hint=Severity.P2)
    return Incident(anomaly=anomaly, status=status,
                    action=RemediationAction(anomaly_id=anomaly.id, adapter="website", action="restart",
                                             severity=Severity.P2))


async def test_webhook_retries_with_stable_idempotency_key(monkeypatch):
    requests = []
    async def no_wait(_):
        pass
    monkeypatch.setattr("ai_sre.core.notifications.asyncio.sleep", no_wait)
    event = incident("recovered")
    def respond(request):
        requests.append(request)
        assert request.headers["Idempotency-Key"] == event.id
        assert json.loads(request.content)["event"] == "incident.recovered"
        return httpx.Response(503 if len(requests) < 3 else 204)
    assert await WebhookNotifier("https://receiver.test/secret", transport=httpx.MockTransport(respond)).send(event)
    assert len(requests) == 3


async def test_failed_delivery_is_not_reported_as_success(caplog):
    assert not await WebhookNotifier("https://receiver.test/secret", transport=httpx.MockTransport(
        lambda r: httpx.Response(400))).send(incident())
    assert "delivery failed" in caplog.text
    assert "secret" not in caplog.text


def test_missing_webhook_env_fails_configuration(monkeypatch):
    monkeypatch.delenv("MISSING_WEBHOOK", raising=False)
    with pytest.raises(ValueError, match="set MISSING_WEBHOOK"):
        WebhookNotifier.from_config({"notifications": {"webhook_url_env": "MISSING_WEBHOOK"}})


def test_report_does_not_claim_an_alert_only_fix_ran():
    report = render_report(incident())
    assert "No remediation was executed" in report
    assert "we fixed it" not in report
    assert "We ran the fix" not in report


@pytest.mark.parametrize("argv", [["ai-sre", "--config", "monitor.yaml", "check"],
                                  ["ai-sre", "check", "--config", "monitor.yaml"]])
def test_config_flag_before_and_after_command(argv, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "monitor.yaml").write_text("integrations: {}\n")
    monkeypatch.setattr("sys.argv", argv)
    async def check(config, **kwargs):
        assert config == {"integrations": {}}
        return 0
    monkeypatch.setattr("ai_sre.__main__._check", check)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0


@pytest.mark.parametrize("ok,code", [(True, 0), (False, 1)])
async def test_check_exit_codes_and_json(ok, code, monkeypatch, capsys):
    from tests.conftest import FakeAdapter
    monkeypatch.setattr("ai_sre.__main__.build_adapters",
                        lambda c: {"test": FakeAdapter("test", {}, {}, ok=ok)})
    assert await _check({}, as_json=True) == code
    assert json.loads(capsys.readouterr().out)[0]["ok"] is ok


def test_state_directory_and_deterministic_reasoner(tmp_path):
    orch = _build_orchestrator({"state_dir": str(tmp_path), "reasoner": "deterministic",
                               "integrations": {"website": {"targets": [{"name": "site", "url": "https://site.test"}]}}},
                              alert_only=True, fake_reasoner=False)
    assert orch.audit.root == tmp_path / "audit"
    assert orch.approvals.root == tmp_path / "approvals"


@pytest.mark.parametrize("config", [{}, {"integrations": {"website": {"interval": 0}}},
                                     {"integrations": {"website": {"interval": float("nan")}}}])
def test_invalid_or_empty_configuration_is_rejected(config):
    with pytest.raises(ValueError):
        build_adapters(config)
