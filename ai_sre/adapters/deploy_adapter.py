"""Bad-deploy / broken-migration adapter (PRD failure #9).

Detection: correlates the app's error rate with the most recent deploy
timestamp. Deploy events are recorded by CI/webhook into a small JSON file
(or the sandbox writes it on `kubectl apply`); error rate comes from the
gateway metrics endpoint. The correlation window lives in rules.py.

Remediation: `kubectl rollout undo` on the affected deployment (the customer's
hosting-platform equivalent would be a different subclass — Vercel API etc.).

Config:
  metrics_url: gateway metrics endpoint (reads error_rate)
  deploy_state_path: JSON file {"deployment": "...", "namespace": "...", "deployed_at": iso8601}
  kubectl_context: optional kube context name
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import httpx

from ..core.adapter import BaseAdapter
from ..core.models import HealthResult, RemediationResult


class DeployAdapter(BaseAdapter):
    name = "deploy"
    default_interval = 60.0
    ACTIONS = {
        "rollback": (
            "Roll the Kubernetes deployment back to the previous revision. "
            "No params required — the deployment/namespace are read from the "
            "recorded deploy state. Optional params: deployment (string), "
            "namespace (string) to override."
        ),
    }

    def _deploy_state(self) -> dict:
        path = Path(self.config["deploy_state_path"])
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    async def check_health(self) -> HealthResult:
        state = self._deploy_state()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(self.config["metrics_url"])
                resp.raise_for_status()
            error_rate = float(resp.json().get("error_rate", 0.0))
        except Exception as exc:
            return HealthResult(adapter=self.name, ok=False, error=str(exc))
        return HealthResult(
            adapter=self.name,
            ok=error_rate < 0.10,
            observed={
                "error_rate": error_rate,
                "last_deploy_at": state.get("deployed_at"),
                "deployment": state.get("deployment"),
                "namespace": state.get("namespace", "default"),
            },
        )

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        if action not in self.ACTIONS:
            return self._unknown_action(action)
        state = self._deploy_state()
        deployment = params.get("deployment") or state.get("deployment")
        namespace = params.get("namespace") or state.get("namespace", "default")
        if not deployment:
            return RemediationResult(ok=False, detail="no deployment recorded to roll back")
        cmd = ["kubectl", "rollout", "undo", f"deployment/{deployment}", "-n", namespace]
        if ctx := self.config.get("kubectl_context"):
            cmd += ["--context", ctx]
        try:
            proc = await asyncio.to_thread(
                subprocess.run, cmd, capture_output=True, text=True, timeout=120
            )
        except Exception as exc:
            return RemediationResult(ok=False, detail=str(exc))
        return RemediationResult(
            ok=proc.returncode == 0,
            detail=(proc.stdout or proc.stderr).strip()[-500:],
        )
