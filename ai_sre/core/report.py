"""Owner-facing incident reports (PRD §2 goal 4).

Plain-English markdown, written for a restaurant owner, not an SRE. One file
per incident under var/incidents/.
"""

from __future__ import annotations

from pathlib import Path

from .models import Incident, Severity

_SEVERITY_LABEL = {
    Severity.P1: "P1 — serious",
    Severity.P2: "P2 — moderate",
    Severity.P3: "P3 — minor",
}

_FRIENDLY_TITLES = {
    "website_unhealthy": "A website health check is failing",
    "github_workflow_failed": "A GitHub Actions workflow needs attention",
    "prometheus_query_unhealthy": "A Prometheus metric threshold is breached",
    "monitor_recovered": "A monitored system is healthy again",
    "stripe_order_mismatch": "A customer was charged but their order didn't come through",
    "stripe_webhook_failing": "Payment notifications from Stripe are failing",
    "db_pool_exhaustion": "The website's database was running out of connections",
    "s3_bucket_public": "Your image storage was accidentally made public",
    "s3_policy_drift": "Your image storage settings changed unexpectedly",
    "s3_object_unreachable": "Images on your site stopped loading",
    "ssl_expiring_soon": "Your site's security certificate is expiring soon",
    "ssl_expiring_urgent": "Your site's security certificate expires in days",
    "domain_expiring": "Your domain name registration is about to lapse",
    "dns_inconsistent": "Your domain is resolving inconsistently",
    "email_bounce_spike": "Emails to customers are bouncing at a high rate",
    "email_quota_near_limit": "You're close to your daily email sending limit",
    "endpoint_abuse": "A bot was hammering part of your website",
    "form_spam": "Your reservation/contact form was being spammed",
    "bad_deploy": "A recent site update appears to have broken something",
    "backup_failure": "Your database backup didn't complete correctly",
    "collector_failure": "One of our monitors couldn't reach your systems",
}


def render_report(incident: Incident) -> str:
    a = incident.anomaly
    title = _FRIENDLY_TITLES.get(a.anomaly_type, a.anomaly_type.replace("_", " "))
    severity = incident.action.severity if incident.action else a.severity_hint
    lines = [
        f"# Incident {incident.id}: {title}",
        "",
        f"- **When detected:** {a.detected_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **Severity:** {_SEVERITY_LABEL[severity]}",
        f"- **System involved:** {a.adapter}",
        f"- **Status:** {incident.status}",
        "",
        "## What happened",
        "",
        incident.action.rationale if incident.action and incident.action.rationale
        else f"Our monitoring detected: {a.anomaly_type.replace('_', ' ')}.",
        "",
        "## What we did",
        "",
    ]
    if incident.status == "recovered":
        lines.append("A new health check passed. No remediation was executed.")
    elif incident.action is None or incident.action.action == "none":
        lines.append("Nothing needed — this turned out to be a false alarm.")
    elif incident.action.action == "escalate":
        lines.append("Recorded for human attention. No remediation was executed. "
                     "External delivery is tracked separately in the notification audit log when configured.")
    elif incident.status == "escalated":
        lines.append(f"Alert-only mode: recorded the proposed fix `{incident.action.action}`. "
                     "No remediation was executed.")
    elif incident.status == "awaiting_approval" and incident.result is not None:
        lines.append(f"We tried the fix `{incident.action.action}` after your approval, but it "
                     f"**did not succeed** — it's back in the queue for you to retry.")
        if incident.result.detail:
            lines += ["", f"Details: {incident.result.detail}"]
        lines += ["", f"Retry with: `ai-sre approve {incident.id}`"]
    elif incident.status == "awaiting_approval":
        lines.append(f"We prepared a fix (`{incident.action.action}`) and are **waiting for your approval** "
                     f"before running it. Approve with: `ai-sre approve {incident.id}`")
    else:
        outcome = "and it succeeded" if incident.result and incident.result.ok else "but it did not succeed"
        lines.append(f"We ran the fix `{incident.action.action}` {outcome}.")
        if incident.result and incident.result.detail:
            lines += ["", f"Details: {incident.result.detail}"]
    lines += ["", "## Technical context (for your developer)", "",
              "```json", a.model_dump_json(indent=2), "```", ""]
    return "\n".join(lines)


def write_report(incident: Incident, root: str | Path = "var/incidents") -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{incident.id}.md"
    path.write_text(render_report(incident))
    return path
