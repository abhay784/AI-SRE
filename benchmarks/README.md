# Reliability benchmarks

Run the website benchmark from the repository root:

```bash
python -m benchmarks.website_reliability \
  --json-output benchmarks/results/website-reliability.json \
  --markdown-output benchmarks/results/website-reliability.md
```

The benchmark injects HTTP 500, HTTP 503, missing-content, and latency-SLO
failures into a local HTTP service. It drives those labeled conditions through
the real website adapter and deterministic rule engine, then reports detection
recall, alert precision, false-positive rate, recovery recall, processing
latency, and measured alert suppression with and without deduplication.

Results measure controlled localhost behavior. They are suitable for describing
the tested reliability pipeline, but must not be presented as production uptime,
Internet latency, customer traffic, or multi-region performance.

With the Docker/kind sandbox running, benchmark the end-to-end email degradation
path through the gateway container, adapter, and rule engine:

```bash
python -m benchmarks.kind_email_reliability \
  --json-output benchmarks/results/kind-email-reliability.json \
  --markdown-output benchmarks/results/kind-email-reliability.md
```
