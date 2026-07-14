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
import socket
import ssl
import subprocess
import tempfile
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
        # Deliberately unverified: we need notAfter even when the chain is
        # untrusted, expired, or self-signed — those are exactly the states
        # this adapter exists to catch, not reasons to fail closed.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=10) as raw_sock:
            with context.wrap_socket(raw_sock, server_hostname=host) as sock:
                der = sock.getpeercert(binary_form=True)
        pem = ssl.DER_cert_to_PEM_cert(der)
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:
            f.write(pem)
            f.flush()
            cert = ssl._ssl._test_decode_cert(f.name)
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
