"""Measured reliability benchmark for the website monitoring pipeline.

Runs the real WebsiteAdapter and RuleEngine against a local HTTP service with
labeled failures. Results describe this controlled benchmark, not production
availability or Internet latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ai_sre.adapters.website_adapter import WebsiteAdapter
from ai_sre.core.models import HealthResult
from ai_sre.core.rules import RuleEngine


FAULTS = {
    "http_500": {"status": 500},
    "http_503": {"status": 503},
    "missing_content": {"body": b"page shell without the expected marker"},
    "latency_slo": {"delay_seconds": 0.025},
}


def percentile(values: list[float], percentile_value: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile_value + 0.999999) - 1))
    return ordered[index]


def website_health(ok: bool) -> HealthResult:
    return HealthResult(
        adapter="website",
        ok=ok,
        observed={"targets": [{
            "resource_id": "benchmark-site",
            "name": "benchmark-site",
            "ok": ok,
            "failure_threshold": 2,
        }]},
    )


def count_failure_alerts(polls: int, dedupe_minutes: int) -> int:
    rules = RuleEngine(dedupe_minutes=dedupe_minutes)
    return sum(
        anomaly.anomaly_type == "website_unhealthy"
        for _ in range(polls)
        for anomaly in rules.evaluate(website_health(False))
    )


class BenchmarkServer:
    def __init__(self) -> None:
        self.state = {"status": 200, "body": b"AbhayOS ready", "delay_seconds": 0.0}
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if state["delay_seconds"]:
                    time.sleep(state["delay_seconds"])
                self.send_response(state["status"])
                self.end_headers()
                self.wfile.write(state["body"])

            def log_message(self, *_args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/"

    def start(self) -> None:
        self.thread.start()

    def healthy(self) -> None:
        self.state.update(status=200, body=b"AbhayOS ready", delay_seconds=0.0)

    def inject(self, fault: str) -> None:
        self.healthy()
        self.state.update(FAULTS[fault])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


async def run_benchmark(*, trials_per_fault: int, healthy_polls: int) -> dict:
    server = BenchmarkServer()
    server.start()
    adapter = WebsiteAdapter({
        "interval": 60,
        "targets": [{
            "name": "benchmark-site",
            "url": server.url,
            "expected_status": 200,
            "contains": "AbhayOS",
            "max_latency_ms": 10,
            "timeout_seconds": 1,
            "failure_threshold": 2,
        }],
    })

    detections = 0
    recoveries = 0
    false_alerts = 0
    detection_ms: list[float] = []
    recovery_ms: list[float] = []
    detections_by_fault = {name: 0 for name in FAULTS}

    try:
        server.healthy()
        healthy_rules = RuleEngine()
        for _ in range(healthy_polls):
            health = await adapter.check_health()
            false_alerts += len(healthy_rules.evaluate(health))

        for fault in FAULTS:
            for _ in range(trials_per_fault):
                rules = RuleEngine()
                server.healthy()
                assert not rules.evaluate(await adapter.check_health())

                server.inject(fault)
                detection_started = time.perf_counter()
                first = rules.evaluate(await adapter.check_health())
                second = rules.evaluate(await adapter.check_health())
                elapsed_ms = (time.perf_counter() - detection_started) * 1000
                detected = not first and len(second) == 1 and second[0].anomaly_type == "website_unhealthy"
                if detected:
                    detections += 1
                    detections_by_fault[fault] += 1
                    detection_ms.append(elapsed_ms)

                server.healthy()
                recovery_started = time.perf_counter()
                recovery = rules.evaluate(await adapter.check_health())
                elapsed_recovery_ms = (time.perf_counter() - recovery_started) * 1000
                if len(recovery) == 1 and recovery[0].anomaly_type == "monitor_recovered":
                    recoveries += 1
                    recovery_ms.append(elapsed_recovery_ms)
    finally:
        server.close()

    total_injections = trials_per_fault * len(FAULTS)
    true_positives = detections
    false_negatives = total_injections - detections
    precision = true_positives / (true_positives + false_alerts) if true_positives + false_alerts else 0
    recall = true_positives / total_injections if total_injections else 0
    false_positive_rate = false_alerts / healthy_polls if healthy_polls else 0

    failed_polls = 30
    without_dedupe = count_failure_alerts(failed_polls, dedupe_minutes=0)
    with_dedupe = count_failure_alerts(failed_polls, dedupe_minutes=30)
    suppression = (without_dedupe - with_dedupe) / without_dedupe if without_dedupe else 0

    recovery_reset_rules = RuleEngine()
    recovery_sequence = [
        recovery_reset_rules.evaluate(website_health(False)),
        recovery_reset_rules.evaluate(website_health(False)),
        recovery_reset_rules.evaluate(website_health(True)),
        recovery_reset_rules.evaluate(website_health(False)),
        recovery_reset_rules.evaluate(website_health(False)),
    ]
    recovery_reset_passed = [len(events) for events in recovery_sequence] == [0, 1, 1, 0, 1]

    return {
        "benchmark": "website reliability pipeline",
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "configuration": {
            "fault_types": list(FAULTS),
            "trials_per_fault": trials_per_fault,
            "healthy_polls": healthy_polls,
            "failure_threshold": 2,
            "production_poll_interval_seconds": 60,
        },
        "results": {
            "injected_outages": total_injections,
            "detected_outages": detections,
            "missed_outages": false_negatives,
            "detection_recall_pct": round(recall * 100, 3),
            "alert_precision_pct": round(precision * 100, 3),
            "healthy_polls": healthy_polls,
            "false_alerts": false_alerts,
            "false_positive_rate_pct": round(false_positive_rate * 100, 3),
            "recovered_outages": recoveries,
            "recovery_recall_pct": round(100 * recoveries / total_injections, 3),
            "detections_by_fault": detections_by_fault,
            "immediate_two_poll_detection_p50_ms": round(statistics.median(detection_ms), 3),
            "immediate_two_poll_detection_p95_ms": round(percentile(detection_ms, 0.95), 3),
            "single_poll_recovery_p50_ms": round(statistics.median(recovery_ms), 3),
            "single_poll_recovery_p95_ms": round(percentile(recovery_ms, 0.95), 3),
            "configured_detection_window_seconds": [60, 120],
            "failed_polls_in_dedupe_test": failed_polls,
            "alerts_without_deduplication": without_dedupe,
            "alerts_with_30_minute_deduplication": with_dedupe,
            "measured_alert_suppression_pct": round(suppression * 100, 3),
            "recovery_reset_passed": recovery_reset_passed,
        },
        "interpretation": {
            "scope": "Controlled localhost faults through the real HTTP adapter and rule engine.",
            "latency": "Immediate-poll processing latency excludes the configured 60-second sleep between production polls.",
            "production": "These results do not establish production uptime, Internet latency, or multi-region behavior.",
        },
    }


def render_markdown(result: dict) -> str:
    config = result["configuration"]
    metrics = result["results"]
    fault_rows = "\n".join(
        f"| `{fault}` | {count}/{config['trials_per_fault']} |"
        for fault, count in metrics["detections_by_fault"].items()
    )
    return f"""# Website reliability benchmark

Measured: {result['measured_at']}

## Results

| Metric | Result |
| --- | ---: |
| Injected outages | {metrics['injected_outages']} |
| Detection recall | {metrics['detection_recall_pct']}% |
| Alert precision | {metrics['alert_precision_pct']}% |
| Healthy polls | {metrics['healthy_polls']} |
| False-positive rate | {metrics['false_positive_rate_pct']}% |
| Recovery recall | {metrics['recovery_recall_pct']}% |
| Immediate two-poll detection, p50 | {metrics['immediate_two_poll_detection_p50_ms']} ms |
| Immediate two-poll detection, p95 | {metrics['immediate_two_poll_detection_p95_ms']} ms |
| Single-poll recovery, p95 | {metrics['single_poll_recovery_p95_ms']} ms |
| Alerts without deduplication | {metrics['alerts_without_deduplication']} |
| Alerts with 30-minute deduplication | {metrics['alerts_with_30_minute_deduplication']} |
| Measured alert suppression | {metrics['measured_alert_suppression_pct']}% |
| Recovery reset verified | {metrics['recovery_reset_passed']} |

## Detection by injected fault

| Fault | Detected trials |
| --- | ---: |
{fault_rows}

## Scope

This benchmark runs controlled localhost faults through the real HTTP adapter
and deterministic rule engine. Processing latency excludes the configured
60-second production polling interval. Results do not establish production
uptime, Internet latency, or multi-region performance.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials-per-fault", type=int, default=50)
    parser.add_argument("--healthy-polls", type=int, default=1000)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()
    if args.trials_per_fault < 1 or args.healthy_polls < 1:
        parser.error("trial and poll counts must be positive")

    result = asyncio.run(run_benchmark(
        trials_per_fault=args.trials_per_fault,
        healthy_polls=args.healthy_polls,
    ))
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
