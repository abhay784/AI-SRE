"""DNS / domain lapse adapter (PRD failure #5).

Detection: registrar API for the domain expiry date + resolution consistency
across multiple public resolvers. This is largely a "don't let the human
forget" case — remediation is escalation only, so ACTIONS is empty.

Config:
  domain: the domain to watch
  expiry_url: registrar API endpoint returning JSON with an `expires_at`
              ISO-8601 field (Cloudflare Registrar / Namecheap wrapper, or the
              sandbox's fake registrar)
  expiry_auth_env: env var whose value is sent as a bearer token (optional)
  resolvers: list of resolver IPs (default: Google, Cloudflare, Quad9)
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import httpx

from ..core.adapter import BaseAdapter
from ..core.models import HealthResult, RemediationResult

DEFAULT_RESOLVERS = ["8.8.8.8", "1.1.1.1", "9.9.9.9"]


class DNSAdapter(BaseAdapter):
    name = "dns"
    default_interval = 86400.0
    ACTIONS: dict[str, str] = {}  # escalate-only failure mode

    async def check_health(self) -> HealthResult:
        observed: dict = {"domain": self.config["domain"]}
        error = None
        try:
            expires_at = await self._fetch_expiry()
            if expires_at:
                observed["days_to_domain_expiry"] = (expires_at - datetime.now(timezone.utc)).days
            answers = await asyncio.to_thread(self._resolve_everywhere)
            observed["resolver_answers"] = answers
            observed["resolver_mismatch"] = len({tuple(sorted(v)) for v in answers.values()}) > 1
        except Exception as exc:
            error = str(exc)
        return HealthResult(
            adapter=self.name,
            ok=error is None and not observed.get("resolver_mismatch")
            and observed.get("days_to_domain_expiry", 999) > 60,
            observed=observed,
            error=error,
        )

    async def _fetch_expiry(self) -> datetime | None:
        url = self.config.get("expiry_url")
        if not url:
            return None
        headers = {}
        if env := self.config.get("expiry_auth_env"):
            headers["Authorization"] = f"Bearer {os.environ[env]}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
        raw = resp.json()["expires_at"]
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def _resolve_everywhere(self) -> dict[str, list[str]]:
        import dns.resolver

        answers: dict[str, list[str]] = {}
        for server in self.config.get("resolvers", DEFAULT_RESOLVERS):
            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = [server]
            resolver.lifetime = 5
            try:
                result = resolver.resolve(self.config["domain"], "A")
                answers[server] = sorted(r.to_text() for r in result)
            except Exception as exc:
                answers[server] = [f"error: {exc}"]
        return answers

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        return self._unknown_action(action)
