"""Append-only audit trail (PRD §8).

Two JSONL streams:
  - llm_calls.jsonl: every reasoning invocation — tokens, cost, latency, decision
  - actions.jsonl:   every remediation taken or proposed, and its result

These files are the raw material for incident reports and the cost dashboard.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, root: str | Path = "var/audit"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _append(self, filename: str, record: dict) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        with open(self.root / filename, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def llm_call(self, *, anomaly_id: str, model: str, input_tokens: int,
                 output_tokens: int, cost_usd: float | None, duration_ms: int | None,
                 decision: dict) -> None:
        self._append("llm_calls.jsonl", {
            "anomaly_id": anomaly_id, "model": model,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost_usd": cost_usd, "duration_ms": duration_ms, "decision": decision,
        })

    def action(self, *, incident_id: str, anomaly_type: str, adapter: str,
               action: str, params: dict, severity: str, status: str,
               detail: str = "", approved_by: str | None = None) -> None:
        self._append("actions.jsonl", {
            "incident_id": incident_id, "anomaly_type": anomaly_type,
            "adapter": adapter, "action": action, "params": params,
            "severity": severity, "status": status, "detail": detail,
            "approved_by": approved_by,
        })

    def read(self, filename: str) -> list[dict]:
        path = self.root / filename
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
