"""Email deliverability adapter (PRD failure #8).

Detection: bounce/complaint rate and daily quota usage. Speaks a generic
metrics endpoint (used by the sandbox's mail stub) or SES via boto3.

Remediation: throttle non-critical email to preserve transactional quota, or
fail over to a secondary provider. Both act on the *app's* admin endpoint —
the app owns its provider config; we just flip the switch.

Config:
  provider: "ses" | "http"
  metrics_url: for provider=http — JSON: {bounce_rate, complaint_rate, quota_used_pct}
  admin_url: app admin endpoint accepting POST {"email_mode": "throttled"|"failover"|"normal"}
  region: for provider=ses
"""

from __future__ import annotations

import asyncio

import httpx

from ..core.adapter import BaseAdapter
from ..core.models import HealthResult, RemediationResult


class EmailAdapter(BaseAdapter):
    name = "email"
    default_interval = 600.0
    ACTIONS = {
        "throttle_noncritical": "Pause marketing/non-critical email so transactional email keeps "
                                "its quota. No params required.",
        "failover_provider": "Switch the app to its secondary email provider (SES <-> SendGrid). "
                             "No params required.",
    }

    async def check_health(self) -> HealthResult:
        try:
            if self.config.get("provider", "http") == "ses":
                observed = await asyncio.to_thread(self._poll_ses)
            else:
                observed = await self._poll_http()
        except Exception as exc:
            return HealthResult(adapter=self.name, ok=False, error=str(exc))
        return HealthResult(
            adapter=self.name,
            ok=observed.get("bounce_rate", 0) < 0.05 and observed.get("quota_used_pct", 0) < 90,
            observed=observed,
        )

    async def _poll_http(self) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(self.config["metrics_url"])
            resp.raise_for_status()
        return resp.json()

    def _poll_ses(self) -> dict:
        import boto3

        ses = boto3.client("ses", region_name=self.config.get("region"))
        quota = ses.get_send_quota()
        stats = ses.get_send_statistics()["SendDataPoints"]
        recent = sorted(stats, key=lambda p: p["Timestamp"])[-6:]  # last ~90 min
        sent = sum(p["DeliveryAttempts"] for p in recent) or 1
        bounces = sum(p["Bounces"] for p in recent)
        complaints = sum(p["Complaints"] for p in recent)
        return {
            "bounce_rate": bounces / sent,
            "complaint_rate": complaints / sent,
            "quota_used_pct": 100.0 * quota["SentLast24Hours"] / max(quota["Max24HourSend"], 1),
        }

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        if action not in self.ACTIONS:
            return self._unknown_action(action)
        mode = "throttled" if action == "throttle_noncritical" else "failover"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(self.config["admin_url"], json={"email_mode": mode})
            return RemediationResult(
                ok=resp.status_code < 300,
                detail=f"app email_mode set to {mode} ({resp.status_code})",
            )
        except Exception as exc:
            return RemediationResult(ok=False, detail=str(exc))
