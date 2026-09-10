"""Application-layer traffic adapter (PRD failures #6 and #7).

Detection: reads the app gateway's /metrics endpoint (per-endpoint, per-IP
request rates + form-submission rate) — this is the post-Cloudflare view that
edge rules can't see.

Remediation: application-level rate limit / IP block, or temporarily enabling
the CAPTCHA/honeypot on forms — via the gateway's admin endpoint.

Config:
  metrics_url: gateway metrics endpoint, JSON:
      {top_ips: {ip: rps}, form_submissions_per_min: n, error_rate: f}
  admin_url: gateway admin endpoint (POST {"block_ip": ...} / {"captcha": true})
"""

from __future__ import annotations

import httpx

from ..core.adapter import BaseAdapter
from ..core.models import HealthResult, RemediationResult


class TrafficAdapter(BaseAdapter):
    name = "traffic"
    default_interval = 60.0
    ACTIONS = {
        "block_ip": (
            "Block a single IP at the application layer. Required param: ip "
            "(string) — copy it verbatim from the anomaly context's ip field."
        ),
        "enable_captcha": "Temporarily require CAPTCHA on public forms and pause downstream calls. "
                          "No params required.",
    }

    async def check_health(self) -> HealthResult:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(self.config["metrics_url"])
                resp.raise_for_status()
            observed = resp.json()
        except Exception as exc:
            return HealthResult(adapter=self.name, ok=False, error=str(exc))
        quiet = (
            all(rps < 10 for rps in observed.get("top_ips", {}).values())
            and observed.get("form_submissions_per_min", 0) < 10
        )
        return HealthResult(adapter=self.name, ok=quiet, observed=observed)

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        if action not in self.ACTIONS:
            return self._unknown_action(action)
        if action == "block_ip" and not params.get("ip"):
            return RemediationResult(ok=False, detail="block_ip requires an ip param")
        payload = {"block_ip": params["ip"]} if action == "block_ip" else {"captcha": True}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(self.config["admin_url"], json=payload)
            return RemediationResult(
                ok=resp.status_code < 300,
                detail=f"{action} applied via gateway admin ({resp.status_code})",
            )
        except Exception as exc:
            return RemediationResult(ok=False, detail=str(exc))
