"""Optional outbound JSON incident webhook with bounded delivery retries."""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlsplit

import httpx

from .models import Incident

log = logging.getLogger("slopsaver")


class WebhookNotifier:
    def __init__(self, url: str, *, transport=None):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("notification webhook must be an HTTP(S) URL without embedded credentials")
        self.url = url
        self.transport = transport

    @classmethod
    def from_config(cls, config: dict):
        env_name = (config.get("notifications") or {}).get("webhook_url_env")
        if not env_name:
            return None
        url = os.getenv(env_name)
        if not url:
            raise ValueError(f"set {env_name} to enable the configured notification webhook")
        return cls(url)

    async def send(self, incident: Incident) -> bool:
        payload = {"event": "incident.recovered" if incident.status == "recovered" else "incident.created",
                   "incident": incident.model_dump(mode="json")}
        async with httpx.AsyncClient(transport=self.transport, timeout=10) as client:
            for attempt in range(3):
                try:
                    async with asyncio.timeout(10):
                        async with client.stream("POST", self.url, json=payload,
                                                 headers={"Idempotency-Key": incident.id}) as response:
                            if 200 <= response.status_code < 300:
                                return True
                            if response.status_code < 500 and response.status_code != 429:
                                break
                except (httpx.HTTPError, TimeoutError):
                    pass  # URLs and exception messages may contain webhook credentials.
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        log.error("notification delivery failed for incident %s; local report retained", incident.id)
        return False
