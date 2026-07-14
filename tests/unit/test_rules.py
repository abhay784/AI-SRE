"""Rule-layer thresholds, severity hints, and dedupe."""

from slopsaver.core.models import HealthResult, Severity
from slopsaver.core.rules import RuleEngine


def _health(adapter: str, observed: dict, error: str | None = None) -> HealthResult:
    return HealthResult(adapter=adapter, ok=False, observed=observed, error=error)


def test_db_pool_thresholds():
    engine = RuleEngine()
    quiet = engine.evaluate(_health("postgres", {"active_connections": 5, "max_connections": 20}))
    assert quiet == []

    warn = engine.evaluate(_health("postgres", {"active_connections": 17, "max_connections": 20}))
    assert [a.anomaly_type for a in warn] == ["db_pool_exhaustion"]
    assert warn[0].severity_hint == Severity.P2

    engine2 = RuleEngine()
    crit = engine2.evaluate(_health("postgres", {"active_connections": 20, "max_connections": 20}))
    assert crit[0].severity_hint == Severity.P1


def test_stripe_mismatch_and_webhook_failures():
    engine = RuleEngine()
    anomalies = engine.evaluate(_health("stripe", {
        "unmatched_charges": [{"id": "ch_1", "amount": 4200}],
        "webhook_error_count": 4,
    }))
    kinds = {a.anomaly_type for a in anomalies}
    assert kinds == {"stripe_order_mismatch", "stripe_webhook_failing"}
    mismatch = next(a for a in anomalies if a.anomaly_type == "stripe_order_mismatch")
    assert mismatch.context["charge_id"] == "ch_1"
    assert mismatch.context["matching_order_found"] is False


def test_s3_public_is_p1():
    engine = RuleEngine()
    anomalies = engine.evaluate(_health("s3", {"policy_drifted": True, "public_access": True}))
    assert anomalies[0].anomaly_type == "s3_bucket_public"
    assert anomalies[0].severity_hint == Severity.P1


def test_ssl_tiers():
    engine = RuleEngine()
    assert engine.evaluate(_health("ssl", {"days_to_expiry": 90})) == []
    soon = engine.evaluate(_health("ssl", {"days_to_expiry": 20}))
    assert soon[0].anomaly_type == "ssl_expiring_soon"
    urgent = RuleEngine().evaluate(_health("ssl", {"days_to_expiry": 2}))
    assert urgent[0].severity_hint == Severity.P1


def test_deploy_correlation_window():
    from datetime import datetime, timedelta, timezone

    engine = RuleEngine()
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    fired = engine.evaluate(_health("deploy", {"error_rate": 0.5, "last_deploy_at": recent}))
    assert [a.anomaly_type for a in fired] == ["bad_deploy"]

    # same error rate but the deploy was hours ago: not deploy-correlated
    engine2 = RuleEngine()
    assert engine2.evaluate(_health("deploy", {"error_rate": 0.5, "last_deploy_at": stale})) == []


def test_dedupe_suppresses_repeat_anomalies():
    engine = RuleEngine()
    observed = {"active_connections": 18, "max_connections": 20}
    assert len(engine.evaluate(_health("postgres", observed))) == 1
    # same condition on the next poll: suppressed within the dedupe window
    assert engine.evaluate(_health("postgres", observed)) == []


def test_collector_failure_produces_anomaly():
    engine = RuleEngine()
    anomalies = engine.evaluate(_health("ssl", {}, error="connection refused"))
    assert [a.anomaly_type for a in anomalies] == ["collector_failure"]
