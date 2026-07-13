"""SSL certificate expiry adapter (PRD failure #4).

Detection: raw TLS handshake, read the cert's notAfter — no third-party tool.
Remediation: trigger an ACME renewal hook if one is configured; otherwise the
rule layer escalates at 30/14/3 days.

Config:
  host: hostname to check
  port: default 443
  renew_command: optional shell command that triggers renewal (e.g. certbot)
"""

from __future__ import annotations

import asyncio
import ssl
import subprocess
from datetime import datetime, timezone

from ..core.adapter import BaseAdapter
from ..core.models import HealthResult, RemediationResult


class SSLAdapter(BaseAdapter):
    name = "ssl"
    default_interval = 86400.0  # daily per PRD §7
    ACTIONS = {
        "renew_certificate": "Run the configured ACME renewal command",
    }

    async def check_health(self) -> HealthResult:
        try:
            return await asyncio.to_thread(self._check_cert)
        except Exception as exc:
            return HealthResult(adapter=self.name, ok=False, error=str(exc))

    def _check_cert(self) -> HealthResult:
        host = self.config["host"]
        port = int(self.config.get("port", 443))
        context = ssl.create_default_context()
        with context.wrap_socket(
            __import__("socket").create_connection((host, port), timeout=10),
            server_hostname=host,
        ) as sock:
            cert = sock.getpeercert()
        not_after = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc
        )
        days = (not_after - datetime.now(timezone.utc)).days
        return HealthResult(
            adapter=self.name,
            ok=days > 30,
            observed={"host": host, "not_after": not_after.isoformat(), "days_to_expiry": days},
        )

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        if action not in self.ACTIONS:
            return self._unknown_action(action)
        cmd = self.config.get("renew_command")
        if not cmd:
            return RemediationResult(ok=False, detail="no renew_command configured; escalate to human")
        proc = await asyncio.to_thread(
            subprocess.run, cmd, shell=True, capture_output=True, text=True, timeout=300
        )
        return RemediationResult(
            ok=proc.returncode == 0,
            detail=(proc.stdout or proc.stderr)[-500:],
        )
