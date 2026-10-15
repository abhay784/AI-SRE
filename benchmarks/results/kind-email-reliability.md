# Docker/kind email reliability benchmark

Measured: 2026-10-15T01:33:13.908114+00:00

| Metric | Result |
| --- | ---: |
| Injected degradation trials | 50 |
| Successful trials | 50 |
| Expected alerts | 100 |
| Correct alerts | 100 |
| Detection recall | 100.0% |
| Alert precision | 100.0% |
| Healthy baselines | 50 |
| False-positive rate | 0.0% |
| Immediate detection, p50 | 15.782 ms |
| Immediate detection, p95 | 21.174 ms |

This benchmark injects email bounce-rate and quota degradation through the
running Docker/kind gateway, collects it through the real email adapter, and
evaluates the deterministic rule engine. Timing excludes the scheduler's poll
interval. These measurements cover this scenario only.
