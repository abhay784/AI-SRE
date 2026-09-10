"""Offline outage -> incident -> recovery demo. No API keys or network."""

import asyncio
from pathlib import Path

import httpx

from ai_sre.adapters.website_adapter import WebsiteAdapter
from ai_sre.core.reasoning import FakeReasoner
from ai_sre.core.scheduler import Orchestrator


async def main():
    status = 503
    adapter = WebsiteAdapter({"targets": [{"name": "storefront", "url": "https://storefront.example/health",
                                          "failure_threshold": 2}]},
                             transport=httpx.MockTransport(lambda request: httpx.Response(status)))
    root = "var/demo"
    orchestrator = Orchestrator({"website": adapter}, FakeReasoner(), alert_only=True, var_root=root)
    print("Simulated storefront outage (HTTP 503)")
    print(f"Poll 1: {len(await orchestrator.poll_once(adapter))} incidents; confirming failure")
    outage, = await orchestrator.poll_once(adapter)
    print(f"Poll 2: {outage.status} — {outage.action.rationale}")
    print(f"Poll 3: {len(await orchestrator.poll_once(adapter))} new incidents; duplicate suppressed")
    status = 200
    recovery, = await orchestrator.poll_once(adapter)
    print(f"Poll 4: {recovery.status} — {recovery.action.rationale}")
    print(f"Reports: {Path(root, 'incidents').resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
