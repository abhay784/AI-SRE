"""Usage/cost dashboard (PRD §8): aggregates the audit JSONL streams into the
per-customer stats the PRD calls for — incidents, LLM spend, autonomous vs
escalated ratio, and MTTD/MTTR where derivable.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from ..core.audit import AuditLog


def summarize(audit: AuditLog) -> dict:
    actions = audit.read("actions.jsonl")
    llm_calls = audit.read("llm_calls.jsonl")
    health_checks = audit.read("health_checks.jsonl")

    by_status = Counter(a["status"] for a in actions)
    incidents = {a["incident_id"] for a in actions}
    resolved = [a for a in actions if a["status"] in ("remediated", "failed")]
    autonomous = sum(1 for a in actions if a["status"] == "remediated" and not a.get("approved_by"))
    escalated = by_status.get("escalated", 0) + by_status.get("awaiting_approval", 0)

    # MTTR proxy: detection-to-resolution for incidents that have both an
    # initial record and a terminal record in the log.
    first_seen: dict[str, datetime] = {}
    resolution_secs = []
    for a in sorted(actions, key=lambda x: x["ts"]):
        ts = datetime.fromisoformat(a["ts"])
        first_seen.setdefault(a["incident_id"], ts)
        if a["status"] in ("remediated", "failed"):
            resolution_secs.append((ts - first_seen[a["incident_id"]]).total_seconds())

    total_cost = sum(c.get("cost_usd") or 0.0 for c in llm_calls)
    total_tokens = sum((c.get("input_tokens") or 0) + (c.get("output_tokens") or 0)
                       for c in llm_calls)

    website_samples: dict[str, list[dict]] = defaultdict(list)
    for check in health_checks:
        if check.get("adapter") != "website":
            continue
        for target in check.get("observed", {}).get("targets", []):
            name = target.get("name") or target.get("resource_id") or "unknown"
            website_samples[str(name)].append(target)

    website_targets = {}
    for name, samples in sorted(website_samples.items()):
        successful = sum(bool(sample.get("ok")) for sample in samples)
        latencies = sorted(
            float(sample["latency_ms"])
            for sample in samples
            if isinstance(sample.get("latency_ms"), (int, float))
        )
        p95_index = max(0, int(len(latencies) * 0.95 + 0.999999) - 1) if latencies else 0
        website_targets[name] = {
            "checks": len(samples),
            "successful_checks": successful,
            "availability_pct": round(100.0 * successful / len(samples), 3),
            "average_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "p95_latency_ms": round(latencies[p95_index], 1) if latencies else None,
        }

    return {
        "incidents": len(incidents),
        "by_status": dict(by_status),
        "resolved_autonomously": autonomous,
        "escalated_to_human": escalated,
        "autonomy_pct": round(100.0 * autonomous / len(incidents), 1) if incidents else None,
        "mean_time_to_remediate_s": round(sum(resolution_secs) / len(resolution_secs), 1)
        if resolution_secs else None,
        "llm_calls": len(llm_calls),
        "llm_tokens": total_tokens,
        "llm_spend_usd": round(total_cost, 4),
        "website_targets": website_targets,
    }


def render(audit: AuditLog) -> str:
    stats = summarize(audit)
    lines = ["AI-SRE dashboard", "=" * 40]
    for key, value in stats.items():
        lines.append(f"{key:28} {value}")
    return "\n".join(lines)
