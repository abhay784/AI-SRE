"""Read-only HTTP probes for explicitly configured websites and health endpoints."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.adapter import BaseAdapter
from ..core.models import HealthResult, RemediationResult


class WebsiteTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    url: str
    expected_status: int = Field(default=200, ge=100, le=599)
    contains: str | None = Field(default=None, min_length=1)
    max_latency_ms: float = Field(default=2000, gt=0, allow_inf_nan=False)
    timeout_seconds: float = Field(default=10, gt=0, le=120, allow_inf_nan=False)
    failure_threshold: int = Field(default=2, ge=1, le=100)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("use an absolute HTTP(S) URL without embedded credentials")
        # Accessing port validates malformed port numbers before polling starts.
        _ = url.port
        return value


class WebsiteAdapter(BaseAdapter):
    name = "website"
    default_interval = 60.0
    ACTIONS = {}  # Monitoring never changes the target website.
    MAX_BODY_BYTES = 1_048_576

    def __init__(self, config: dict, *, transport=None):
        super().__init__(config)
        self.targets = [WebsiteTarget.model_validate(t) for t in config.get("targets", [])]
        if not self.targets or len({t.name for t in self.targets}) != len(self.targets):
            raise ValueError("website.targets must contain targets with unique names")
        self.transport = transport

    async def check_health(self) -> HealthResult:
        semaphore = asyncio.Semaphore(5)
        async with httpx.AsyncClient(
            transport=self.transport, follow_redirects=True, max_redirects=5,
            headers={"User-Agent": "AI-SRE/0.2 (+https://github.com/abhay784/AI-SRE)"},
        ) as client:
            async def probe(target):
                async with semaphore:
                    return await self._probe(client, target)

            targets = await asyncio.gather(*(probe(t) for t in self.targets))
        return HealthResult(adapter=self.name, ok=all(t["ok"] for t in targets),
                            observed={"targets": targets})

    async def _probe(self, client: httpx.AsyncClient, target: WebsiteTarget) -> dict:
        url = urlsplit(target.url)
        result = {
            "resource_id": target.name, "name": target.name,
            # Query strings can contain signed credentials; never persist them.
            "url": urlunsplit((url.scheme, url.netloc, url.path, "", "")),
            "expected_status": target.expected_status,
            "failure_threshold": target.failure_threshold,
            "max_latency_ms": target.max_latency_ms,
            "ok": False, "issues": [],
        }
        started = time.monotonic()
        try:
            async with asyncio.timeout(target.timeout_seconds):
                async with client.stream("GET", target.url, timeout=target.timeout_seconds) as response:
                    result["status_code"] = response.status_code
                    if response.status_code != target.expected_status:
                        result["issues"].append(f"expected HTTP {target.expected_status}, got {response.status_code}")
                    if target.contains is not None:
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            body.extend(chunk[:self.MAX_BODY_BYTES - len(body)])
                            if len(body) >= self.MAX_BODY_BYTES:
                                break
                        matched = target.contains in body.decode(response.encoding or "utf-8", errors="replace")
                        result["content_matched"] = matched
                        if not matched:
                            result["issues"].append("expected text missing from first 1 MiB of response")
            result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            if result["latency_ms"] > target.max_latency_ms:
                result["issues"].append(f"response exceeded {target.max_latency_ms:g} ms")
        except (TimeoutError, httpx.TimeoutException):
            result["issues"].append("request timed out")
        except httpx.HTTPError as exc:
            # HTTPX exception strings can include the secret-bearing request URL.
            result["issues"].append(f"request failed ({type(exc).__name__})")
        result["ok"] = not result["issues"]
        return result

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        return self._unknown_action(action)
