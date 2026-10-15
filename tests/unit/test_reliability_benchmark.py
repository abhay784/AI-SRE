from benchmarks.website_reliability import count_failure_alerts, percentile


def test_deduplication_benchmark_counts_alerts_after_confirmation():
    assert count_failure_alerts(30, dedupe_minutes=0) == 29
    assert count_failure_alerts(30, dedupe_minutes=30) == 1


def test_benchmark_percentile_uses_nearest_rank():
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert percentile([1, 2, 3, 4, 5], 0.95) == 5
