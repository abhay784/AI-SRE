"""Read-only Prometheus instant-query monitoring for configured PromQL thresholds."""

from __future__ import annotations

import asyncio
import json
import math
import os
from statistics import fmean
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.adapter import BaseAdapter
from ..core.models import HealthResult, RemediationResult, Severity


class PrometheusQuery(BaseModel):
    """A single instant PromQL expression and its expected numeric bound."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=8_000)
    operator: Literal["gt", "gte", "lt", "lte"]
    threshold: float = Field(allow_inf_nan=False)
    reducer: Literal["max", "min", "sum", "avg"] = "max"
    severity: Severity = Severity.P2
    failure_threshold: int = Field(default=2, ge=1, le=100)
    no_data_is_failure: bool = True


class PrometheusAdapter(BaseAdapter):
    """Queries a Prometheus-compatible `/api/v1/query` endpoint.

    Queries must return a scalar or instant vector. When a query returns more
    than one series, its configured reducer turns it into a single number.
    """

    name = "prometheus"
    default_interval = 60.0
    ACTIONS = {}  # This integration has no write or remediation privileges.
    MAX_RESPONSE_BYTES = 1_048_576

    def __init__(self, config: dict, *, transport=None):
        super().__init__(config)
        self.url = self._validate_url(config.get("url"))
        self.queries = [PrometheusQuery.model_validate(q) for q in config.get("queries", [])]
        if not self.queries or len({q.name for q in self.queries}) != len(self.queries):
            raise ValueError("prometheus.queries must contain queries with unique names")
        self.timeout_seconds = self._positive_float(config.get("timeout_seconds", 10), "timeout_seconds", 120)
        self.query_timeout_seconds = self._positive_float(
            config.get("query_timeout_seconds", self.timeout_seconds), "query_timeout_seconds", self.timeout_seconds
        )
        self.token_env = config.get("bearer_token_env")
        if self.token_env is not None and (not isinstance(self.token_env, str) or not self.token_env):
            raise ValueError("bearer_token_env must name an environment variable")
        self.transport = transport

    @staticmethod
    def _positive_float(value, name: str, maximum: float) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"prometheus.{name} must be a positive finite number") from exc
        if not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError(f"prometheus.{name} must be greater than 0 and at most {maximum:g}")
        return value

    @staticmethod
    def _validate_url(value) -> str:
        if not isinstance(value, str):
            raise ValueError("prometheus.url must be an absolute HTTP(S) URL")
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("prometheus.url must be an absolute HTTP(S) URL without embedded credentials")
        _ = url.port
        if url.query or url.fragment:
            raise ValueError("prometheus.url must not include a query string or fragment")
        return urlunsplit((url.scheme, url.netloc, url.path.rstrip("/"), "", ""))

    async def check_health(self) -> HealthResult:
        headers = {"Accept": "application/json", "User-Agent": "AI-SRE/0.2"}
        token = os.getenv(self.token_env) if self.token_env else None
        if self.token_env and not token:
            return HealthResult(adapter=self.name, ok=False,
                                error=f"configured bearer token environment variable {self.token_env} is not set")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        semaphore = asyncio.Semaphore(5)
        async with httpx.AsyncClient(base_url=f"{self.url}/", headers=headers,
                                     timeout=self.timeout_seconds, transport=self.transport) as client:
            async def query(item):
                async with semaphore:
                    return await self._query(client, item)
            results = await asyncio.gather(*(query(item) for item in self.queries))
        return HealthResult(adapter=self.name, ok=all(result["ok"] for result in results),
                            observed={"url": self.url, "queries": results})

    async def _query(self, client: httpx.AsyncClient, item: PrometheusQuery) -> dict:
        result = {
            "resource_id": item.name,
            "name": item.name,
            "operator": item.operator,
            "threshold": item.threshold,
            "reducer": item.reducer,
            "severity": item.severity.value,
            "failure_threshold": item.failure_threshold,
            "no_data_is_failure": item.no_data_is_failure,
            "ok": False,
            "issues": [],
        }
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with client.stream(
                    "POST", "api/v1/query",
                    data={"query": item.query, "timeout": f"{self.query_timeout_seconds:g}s", "limit": "100"},
                ) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        result["issues"].append(f"Prometheus HTTP {response.status_code}")
                        return result
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        body.extend(chunk[:self.MAX_RESPONSE_BYTES - len(body)])
                        if len(body) >= self.MAX_RESPONSE_BYTES:
                            result["issues"].append("Prometheus response exceeded 1 MiB")
                            return result
            payload = json.loads(body)
            if payload.get("status") != "success":
                result["issues"].append("Prometheus rejected or could not execute the query")
                return result
            values = self._values(payload.get("data", {}))
            if not values:
                if item.no_data_is_failure:
                    result["issues"].append("query returned no data")
                    return result
                result["ok"] = True
                return result
            value = self._reduce(values, item.reducer)
            result.update(value=value, series_count=len(values))
            if self._breaches(value, item.operator, item.threshold):
                result["issues"].append(f"value {value:g} breached {item.operator} {item.threshold:g}")
            else:
                result["ok"] = True
        except (TimeoutError, httpx.TimeoutException):
            result["issues"].append("Prometheus query timed out")
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            # Never include exception messages: HTTPX can include URLs or request details.
            result["issues"].append("Prometheus query returned an invalid response")
        return result

    @staticmethod
    def _values(data: dict) -> list[float]:
        result_type, raw = data.get("resultType"), data.get("result")
        if result_type == "scalar":
            raw_values = [raw]
        elif result_type == "vector":
            raw_values = [sample.get("value") for sample in raw]
        else:
            raise ValueError("PromQL must return an instant scalar or vector")
        values = [float(sample[1]) for sample in raw_values]
        if any(not math.isfinite(value) for value in values):
            raise ValueError("PromQL returned a non-finite value")
        return values

    @staticmethod
    def _reduce(values: list[float], reducer: str) -> float:
        return {"max": max, "min": min, "sum": sum, "avg": fmean}[reducer](values)

    @staticmethod
    def _breaches(value: float, operator: str, threshold: float) -> bool:
        return {
            "gt": value > threshold,
            "gte": value >= threshold,
            "lt": value < threshold,
            "lte": value <= threshold,
        }[operator]

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        return self._unknown_action(action)
