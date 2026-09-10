"""Dead-man's-switch heartbeat (PRD §8).

Pings an external URL (Healthchecks.io-style) every N seconds. If AI-SRE
itself dies, the pings stop and the *external* service pages the operator —
monitoring for the monitor.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("ai_sre.heartbeat")


async def heartbeat_loop(url: str, interval: float = 60.0) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                await client.get(url)
            except Exception as exc:
                # Never crash the orchestrator over a heartbeat hiccup.
                log.warning("heartbeat ping failed: %s", exc)
            await asyncio.sleep(interval)
