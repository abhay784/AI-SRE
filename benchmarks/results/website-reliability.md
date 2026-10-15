# Website reliability benchmark

Measured: 2026-10-15T01:31:20.283833+00:00

## Results

| Metric | Result |
| --- | ---: |
| Injected outages | 200 |
| Detection recall | 100.0% |
| Alert precision | 100.0% |
| Healthy polls | 1000 |
| False-positive rate | 0.0% |
| Recovery recall | 100.0% |
| Immediate two-poll detection, p50 | 22.98 ms |
| Immediate two-poll detection, p95 | 84.432 ms |
| Single-poll recovery, p95 | 13.429 ms |
| Alerts without deduplication | 29 |
| Alerts with 30-minute deduplication | 1 |
| Measured alert suppression | 96.552% |
| Recovery reset verified | True |

## Detection by injected fault

| Fault | Detected trials |
| --- | ---: |
| `http_500` | 50/50 |
| `http_503` | 50/50 |
| `missing_content` | 50/50 |
| `latency_slo` | 50/50 |

## Scope

This benchmark runs controlled localhost faults through the real HTTP adapter
and deterministic rule engine. Processing latency excludes the configured
60-second production polling interval. Results do not establish production
uptime, Internet latency, or multi-region performance.
