"""DB connection-pool adapter (PRD failure #2).

Detection: poll pg_stat_activity vs max_connections with a read-only role.
Remediation: terminate idle-in-transaction backends via a separately-scoped
kill role (never the monitoring role).

Config:
  dsn: monitoring DSN (read-only role)
  kill_dsn: DSN with pg_terminate_backend rights (optional; kill disabled without it)
  idle_kill_seconds: how long idle-in-transaction before eligible (default 300)
"""

from __future__ import annotations

import asyncio

from ..core.adapter import BaseAdapter
from ..core.models import HealthResult, RemediationResult


class PostgresAdapter(BaseAdapter):
    name = "postgres"
    default_interval = 30.0  # every 30s per PRD §7
    ACTIONS = {
        "kill_idle_connections": (
            "Terminate idle-in-transaction backends older than the configured age. "
            "No params required. Optional param: idle_seconds (integer) to override "
            "the default threshold for this run."
        ),
    }

    async def check_health(self) -> HealthResult:
        try:
            return await asyncio.to_thread(self._poll)
        except Exception as exc:
            return HealthResult(adapter=self.name, ok=False, error=str(exc))

    def _poll(self) -> HealthResult:
        import psycopg

        with psycopg.connect(self.config["dsn"], connect_timeout=5) as conn:
            active, idle_in_tx = conn.execute(
                "SELECT count(*), "
                "count(*) FILTER (WHERE state = 'idle in transaction') "
                "FROM pg_stat_activity WHERE backend_type = 'client backend'"
            ).fetchone()
            (max_conn,) = conn.execute("SHOW max_connections").fetchone()
        max_conn = int(max_conn)
        return HealthResult(
            adapter=self.name,
            ok=active / max_conn < 0.8,
            observed={
                "active_connections": active,
                "max_connections": max_conn,
                "idle_in_transaction": idle_in_tx,
            },
        )

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        if action not in self.ACTIONS:
            return self._unknown_action(action)
        if not self.config.get("kill_dsn"):
            return RemediationResult(ok=False, detail="kill_dsn not configured; connection-kill disabled")
        try:
            return await asyncio.to_thread(self._kill_idle, params)
        except Exception as exc:
            return RemediationResult(ok=False, detail=str(exc))

    def _kill_idle(self, params: dict) -> RemediationResult:
        import psycopg

        max_age = int(params.get("idle_seconds", self.config.get("idle_kill_seconds", 300)))
        with psycopg.connect(self.config["kill_dsn"], connect_timeout=5) as conn:
            rows = conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE state = 'idle in transaction' "
                "AND state_change < now() - make_interval(secs => %s) "
                "AND pid <> pg_backend_pid()",
                (max_age,),
            ).fetchall()
        return RemediationResult(ok=True, detail=f"terminated {len(rows)} idle-in-transaction backends")
