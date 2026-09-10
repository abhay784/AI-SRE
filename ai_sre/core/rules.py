"""Deterministic correlation / anomaly layer (PRD §5).

Cheap threshold rules that run on every poll. No LLM calls here — an
AnomalyEvent is the only thing that wakes the reasoning agent, so these rules
are the gatekeeper for cost and for false-positive rate.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

from .models import AnomalyEvent, HealthResult, Severity

# Defaults; each can be overridden per-customer in config under `thresholds:`.
DEFAULT_THRESHOLDS = {
    "db_pool_pct": 80.0,          # % of max_connections in use
    "ssl_warn_days": 30,          # first cert-expiry escalation
    "ssl_urgent_days": 3,
    "dns_warn_days": 60,
    "email_bounce_rate": 0.05,    # 5% bounce rate
    "email_quota_pct": 90.0,
    "endpoint_rps": 10.0,         # per-IP request rate on app endpoints
    "form_spam_per_min": 10,      # form submissions per minute
    "deploy_error_rate": 0.10,    # error rate within window after a deploy
    "deploy_window_min": 15,
    "backup_max_age_hours": 26,
}


class RuleEngine:
    """Evaluates each HealthResult against per-adapter rules.

    Keeps a short in-memory window per adapter so cross-poll correlation
    (e.g. error-rate-after-deploy) is possible, and dedupes so a persistent
    condition fires one anomaly, not one per poll.
    """

    def __init__(self, thresholds: dict | None = None, dedupe_minutes: int = 30):
        self.t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self.history: dict[str, deque[HealthResult]] = {}
        self._recent: dict[tuple[str, str, str], datetime] = {}
        self._streaks: dict[tuple[str, str, str], int] = {}
        self._active: set[tuple[str, str, str]] = set()
        self.dedupe = timedelta(minutes=dedupe_minutes)

    def evaluate(self, health: HealthResult) -> list[AnomalyEvent]:
        self.history.setdefault(health.adapter, deque(maxlen=50)).append(health)
        handler = getattr(self, f"_rule_{health.adapter}", None)
        anomalies = handler(health) if handler and not health.error else []
        if health.error:
            anomalies.append(self._anomaly(health, "collector_failure", Severity.P3,
                                           {"error": health.error}))
        return [a for a in anomalies if a is not None and self._not_duplicate(a)]

    # -- dedupe -------------------------------------------------------------

    def _not_duplicate(self, anomaly: AnomalyEvent) -> bool:
        key = (anomaly.adapter, anomaly.anomaly_type, str(anomaly.context.get("resource_id", "")))
        last = self._recent.get(key)
        now = datetime.now(timezone.utc)
        if last and now - last < self.dedupe:
            return False
        self._recent[key] = now
        return True

    def _anomaly(self, h: HealthResult, kind: str, sev: Severity, extra: dict) -> AnomalyEvent:
        return AnomalyEvent(adapter=h.adapter, anomaly_type=kind, severity_hint=sev,
                            context={**h.observed, **extra})

    # -- per-adapter rules ----------------------------------------------------

    def _condition(self, h: HealthResult, kind: str, context: dict, *,
                   unhealthy: bool, threshold: int = 1,
                   severity: Severity = Severity.P2) -> list[AnomalyEvent]:
        resource = str(context["resource_id"])
        key = (h.adapter, kind, resource)
        recovery_key = (h.adapter, "monitor_recovered", resource)
        if unhealthy:
            self._streaks[key] = min(self._streaks.get(key, 0) + 1, threshold)
            if self._streaks[key] < threshold:
                return []
            self._active.add(key)
            self._recent.pop(recovery_key, None)
            return [AnomalyEvent(adapter=h.adapter, anomaly_type=kind, severity_hint=severity,
                                 context={**context, "consecutive_failures": self._streaks[key]})]
        self._streaks.pop(key, None)
        self._recent.pop(key, None)
        if key in self._active:
            self._active.remove(key)
            return [AnomalyEvent(adapter=h.adapter, anomaly_type="monitor_recovered",
                                 severity_hint=Severity.P3,
                                 context={**context, "recovered_from": kind})]
        return []

    def _rule_website(self, h: HealthResult) -> list[AnomalyEvent]:
        out = []
        for target in h.observed.get("targets", []):
            out.extend(self._condition(h, "website_unhealthy", target,
                                      unhealthy=not target["ok"],
                                      threshold=target.get("failure_threshold", 2)))
        return out

    def _rule_github(self, h: HealthResult) -> list[AnomalyEvent]:
        out = []
        for repository in h.observed.get("repositories", []):
            context = {k: v for k, v in repository.items() if k != "runs"}
            out.extend(self._condition(h, "collector_failure", context,
                                      unhealthy=bool(repository.get("error"))))
            # Partial API responses must not resolve previously failing workflows.
            for run in repository.get("runs", []):
                out.extend(self._condition(h, "github_workflow_failed", run,
                                          unhealthy=not run["ok"]))
        return out

    def _rule_prometheus(self, h: HealthResult) -> list[AnomalyEvent]:
        out = []
        for query in h.observed.get("queries", []):
            out.extend(self._condition(
                h, "prometheus_query_unhealthy", query,
                unhealthy=not query["ok"],
                threshold=query.get("failure_threshold", 2),
                severity=Severity(query.get("severity", Severity.P2.value)),
            ))
        return out

    def _rule_stripe(self, h: HealthResult) -> list[AnomalyEvent]:
        out = []
        for charge in h.observed.get("unmatched_charges", []):
            out.append(self._anomaly(h, "stripe_order_mismatch", Severity.P2, {
                "charge_id": charge.get("id"),
                "amount": charge.get("amount"),
                "matching_order_found": False,
            }))
        if h.observed.get("webhook_error_count", 0) >= 3:
            out.append(self._anomaly(h, "stripe_webhook_failing", Severity.P1, {}))
        return out

    def _rule_postgres(self, h: HealthResult) -> list[AnomalyEvent]:
        used, cap = h.observed.get("active_connections", 0), h.observed.get("max_connections", 1)
        pct = 100.0 * used / max(cap, 1)
        if pct >= self.t["db_pool_pct"]:
            sev = Severity.P1 if pct >= 95 else Severity.P2
            return [self._anomaly(h, "db_pool_exhaustion", sev, {"pool_used_pct": round(pct, 1)})]
        return []

    def _rule_s3(self, h: HealthResult) -> list[AnomalyEvent]:
        out = []
        if h.observed.get("policy_drifted"):
            sev = Severity.P1 if h.observed.get("public_access") else Severity.P2
            kind = "s3_bucket_public" if h.observed.get("public_access") else "s3_policy_drift"
            out.append(self._anomaly(h, kind, sev, {}))
        if h.observed.get("synthetic_fetch_ok") is False:
            out.append(self._anomaly(h, "s3_object_unreachable", Severity.P2, {}))
        return out

    def _rule_ssl(self, h: HealthResult) -> list[AnomalyEvent]:
        days = h.observed.get("days_to_expiry")
        if days is None:
            return []
        if days <= self.t["ssl_urgent_days"]:
            return [self._anomaly(h, "ssl_expiring_urgent", Severity.P1, {})]
        if days <= self.t["ssl_warn_days"]:
            return [self._anomaly(h, "ssl_expiring_soon", Severity.P3, {})]
        return []

    def _rule_dns(self, h: HealthResult) -> list[AnomalyEvent]:
        out = []
        days = h.observed.get("days_to_domain_expiry")
        if days is not None and days <= self.t["dns_warn_days"]:
            sev = Severity.P1 if days <= 7 else Severity.P3
            out.append(self._anomaly(h, "domain_expiring", sev, {}))
        if h.observed.get("resolver_mismatch"):
            out.append(self._anomaly(h, "dns_inconsistent", Severity.P2, {}))
        return out

    def _rule_email(self, h: HealthResult) -> list[AnomalyEvent]:
        out = []
        if h.observed.get("bounce_rate", 0.0) >= self.t["email_bounce_rate"]:
            out.append(self._anomaly(h, "email_bounce_spike", Severity.P2, {}))
        if h.observed.get("quota_used_pct", 0.0) >= self.t["email_quota_pct"]:
            out.append(self._anomaly(h, "email_quota_near_limit", Severity.P2, {}))
        return out

    def _rule_traffic(self, h: HealthResult) -> list[AnomalyEvent]:
        out = []
        for ip, rps in h.observed.get("top_ips", {}).items():
            if rps >= self.t["endpoint_rps"]:
                out.append(self._anomaly(h, "endpoint_abuse", Severity.P3,
                                         {"ip": ip, "rps": rps}))
        if h.observed.get("form_submissions_per_min", 0) >= self.t["form_spam_per_min"]:
            out.append(self._anomaly(h, "form_spam", Severity.P3, {}))
        return out

    def _rule_deploy(self, h: HealthResult) -> list[AnomalyEvent]:
        deployed_at = h.observed.get("last_deploy_at")
        rate = h.observed.get("error_rate", 0.0)
        if not deployed_at or rate < self.t["deploy_error_rate"]:
            return []
        deployed = datetime.fromisoformat(deployed_at)
        if datetime.now(timezone.utc) - deployed <= timedelta(minutes=self.t["deploy_window_min"]):
            return [self._anomaly(h, "bad_deploy", Severity.P1, {"error_rate": rate})]
        return []

    def _rule_backup(self, h: HealthResult) -> list[AnomalyEvent]:
        age = h.observed.get("backup_age_hours")
        restore_ok = h.observed.get("test_restore_ok", True)
        if (age is not None and age > self.t["backup_max_age_hours"]) or not restore_ok:
            return [self._anomaly(h, "backup_failure", Severity.P2, {})]
        return []
