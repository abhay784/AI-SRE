"""End-to-end benchmark against the Docker/kind sandbox email failure path."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ai_sre.adapters.email_adapter import EmailAdapter
from ai_sre.core.rules import RuleEngine
from benchmarks.website_reliability import percentile


EXPECTED_ANOMALIES = {"email_bounce_spike", "email_quota_near_limit"}


async def set_metrics(client: httpx.AsyncClient, admin_url: str, *, bounce: float, quota: float) -> None:
    response = await client.post(admin_url, json={
        "email_metrics": {"bounce_rate": bounce, "quota_used_pct": quota},
    })
    response.raise_for_status()


async def run_benchmark(gateway_url: str, trials: int) -> dict:
    gateway_url = gateway_url.rstrip("/")
    admin_url = f"{gateway_url}/admin"
    adapter = EmailAdapter({
        "provider": "http",
        "metrics_url": f"{gateway_url}/email/metrics",
        "admin_url": admin_url,
    })
    successful_trials = 0
    false_alerts = 0
    emitted_alerts = 0
    detection_ms: list[float] = []

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            for _ in range(trials):
                rules = RuleEngine()
                await set_metrics(client, admin_url, bounce=0.01, quota=12.0)
                healthy = await adapter.check_health()
                false_alerts += len(rules.evaluate(healthy))

                started = time.perf_counter()
                await set_metrics(client, admin_url, bounce=0.12, quota=95.0)
                degraded = await adapter.check_health()
                anomalies = rules.evaluate(degraded)
                elapsed_ms = (time.perf_counter() - started) * 1000
                kinds = {anomaly.anomaly_type for anomaly in anomalies}
                emitted_alerts += len(anomalies)
                if kinds == EXPECTED_ANOMALIES:
                    successful_trials += 1
                    detection_ms.append(elapsed_ms)
        finally:
            await set_metrics(client, admin_url, bounce=0.01, quota=12.0)

    expected_alerts = trials * len(EXPECTED_ANOMALIES)
    correct_alerts = successful_trials * len(EXPECTED_ANOMALIES)
    precision = correct_alerts / emitted_alerts if emitted_alerts else 0
    recall = correct_alerts / expected_alerts if expected_alerts else 0
    return {
        "benchmark": "Docker/kind email degradation path",
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "configuration": {
            "gateway_url": gateway_url,
            "trials": trials,
            "expected_anomalies_per_trial": sorted(EXPECTED_ANOMALIES),
        },
        "results": {
            "injected_trials": trials,
            "successful_trials": successful_trials,
            "expected_alerts": expected_alerts,
            "correct_alerts": correct_alerts,
            "unexpected_alerts": emitted_alerts - correct_alerts,
            "healthy_baselines": trials,
            "false_alerts": false_alerts,
            "detection_recall_pct": round(recall * 100, 3),
            "alert_precision_pct": round(precision * 100, 3),
            "false_positive_rate_pct": round(100 * false_alerts / trials, 3),
            "immediate_detection_p50_ms": round(statistics.median(detection_ms), 3),
            "immediate_detection_p95_ms": round(percentile(detection_ms, 0.95), 3),
        },
        "interpretation": {
            "scope": "HTTP fault injection into the running Docker/kind gateway, adapter collection, and deterministic rule evaluation.",
            "latency": "Includes fault injection and immediate collection, but excludes the configured scheduler interval.",
            "coverage": "Measures the email degradation scenario only; it is not an accuracy claim for every sandbox adapter.",
        },
    }


def render_markdown(result: dict) -> str:
    metrics = result["results"]
    return f"""# Docker/kind email reliability benchmark

Measured: {result['measured_at']}

| Metric | Result |
| --- | ---: |
| Injected degradation trials | {metrics['injected_trials']} |
| Successful trials | {metrics['successful_trials']} |
| Expected alerts | {metrics['expected_alerts']} |
| Correct alerts | {metrics['correct_alerts']} |
| Detection recall | {metrics['detection_recall_pct']}% |
| Alert precision | {metrics['alert_precision_pct']}% |
| Healthy baselines | {metrics['healthy_baselines']} |
| False-positive rate | {metrics['false_positive_rate_pct']}% |
| Immediate detection, p50 | {metrics['immediate_detection_p50_ms']} ms |
| Immediate detection, p95 | {metrics['immediate_detection_p95_ms']} ms |

This benchmark injects email bounce-rate and quota degradation through the
running Docker/kind gateway, collects it through the real email adapter, and
evaluates the deterministic rule engine. Timing excludes the scheduler's poll
interval. These measurements cover this scenario only.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway-url", default="http://localhost:30080")
    parser.add_argument("--trials", type=int, default=50)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("trials must be positive")

    result = asyncio.run(run_benchmark(args.gateway_url, args.trials))
    serialized = json.dumps(result, indent=2) + "\n"
    print(serialized, end="")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(serialized)
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(render_markdown(result))


if __name__ == "__main__":
    main()
