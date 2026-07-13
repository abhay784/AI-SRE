"""Reasoning layer — root cause / severity / remediation choice (PRD §5, §7).

Built on the Claude Agent SDK. The agent is deliberately boxed in:
  - it gets exactly two tools (fetch more context, propose a remediation);
  - it can only propose actions from the adapter's ACTIONS allowlist;
  - it never executes side effects — the orchestrator does, after severity
    gating (see approvals.py).

`FakeReasoner` provides deterministic decisions for tests/CI and as a
degraded-mode fallback when the API is unreachable.
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable, Protocol

from .audit import AuditLog
from .models import AnomalyEvent, RemediationAction, Severity

ContextProvider = Callable[[str], Awaitable[dict]]

SYSTEM_PROMPT = """\
You are the reasoning core of SlopSaver, a reliability agent that protects a
small business's website (think: a restaurant with online ordering). A
deterministic monitor detected an anomaly and woke you up. Your job:

1. Determine the likely root cause and blast radius from the context packet.
   Call get_adapter_context if you need more signal.
2. Choose ONE remediation action from the allowlist you are given, or
   "escalate" if no listed action is safe, or "none" if this is a false alarm.
3. Assign severity: P3 = low impact, safe to auto-fix. P2 = moderate impact,
   auto-fix but notify the owner. P1 = user-facing / financial / security
   impact — a human must approve before anything runs.

Anything involving customer payments, data exposure, or taking the site down
is P1. When uncertain between two tiers, pick the more severe one.

You MUST finish by calling propose_remediation exactly once.
"""


class Reasoner(Protocol):
    async def decide(self, anomaly: AnomalyEvent, allowed_actions: dict[str, str]) -> RemediationAction: ...


# Deterministic fallback decisions, keyed by anomaly_type. Used by FakeReasoner
# (tests) and by ClaudeReasoner when the API call fails — a monitoring agent
# that goes dark because the LLM is down is worse than a dumb one.
DEFAULT_DECISIONS: dict[str, tuple[str, Severity]] = {
    "stripe_order_mismatch": ("replay_webhook", Severity.P2),
    "stripe_webhook_failing": ("escalate", Severity.P1),
    "db_pool_exhaustion": ("kill_idle_connections", Severity.P2),
    "s3_policy_drift": ("revert_policy", Severity.P2),
    "s3_bucket_public": ("revert_policy", Severity.P1),
    "s3_object_unreachable": ("escalate", Severity.P2),
    "ssl_expiring_soon": ("renew_certificate", Severity.P3),
    "ssl_expiring_urgent": ("escalate", Severity.P1),
    "domain_expiring": ("escalate", Severity.P1),
    "dns_inconsistent": ("escalate", Severity.P2),
    "email_bounce_spike": ("throttle_noncritical", Severity.P2),
    "email_quota_near_limit": ("throttle_noncritical", Severity.P2),
    "endpoint_abuse": ("block_ip", Severity.P3),
    "form_spam": ("enable_captcha", Severity.P3),
    "bad_deploy": ("rollback", Severity.P1),
    "backup_failure": ("escalate", Severity.P2),
    "collector_failure": ("none", Severity.P3),
}


class FakeReasoner:
    """Table-driven reasoner for tests, CI, and LLM-down degraded mode."""

    async def decide(self, anomaly: AnomalyEvent, allowed_actions: dict[str, str]) -> RemediationAction:
        action, severity = DEFAULT_DECISIONS.get(
            anomaly.anomaly_type, ("escalate", anomaly.severity_hint)
        )
        if action not in allowed_actions and action not in ("escalate", "none"):
            action = "escalate"
        return RemediationAction(
            anomaly_id=anomaly.id, adapter=anomaly.adapter, action=action,
            severity=severity, rationale="deterministic default decision",
        )


class ClaudeReasoner:
    """Runs the Claude Agent SDK loop for one anomaly and returns the decision."""

    MODEL = "claude-opus-4-8"

    def __init__(self, context_provider: ContextProvider, audit: AuditLog):
        self.context_provider = context_provider
        self.audit = audit
        self.fallback = FakeReasoner()

    async def decide(self, anomaly: AnomalyEvent, allowed_actions: dict[str, str]) -> RemediationAction:
        try:
            return await self._decide_with_claude(anomaly, allowed_actions)
        except Exception as exc:  # LLM outage must not blind the monitor
            decision = await self.fallback.decide(anomaly, allowed_actions)
            decision.rationale = f"LLM unavailable ({exc}); deterministic fallback used"
            return decision

    async def _decide_with_claude(self, anomaly: AnomalyEvent,
                                  allowed_actions: dict[str, str]) -> RemediationAction:
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ResultMessage,
            create_sdk_mcp_server,
            query,
            tool,
        )

        proposal: dict = {}
        context_provider = self.context_provider

        @tool(
            "get_adapter_context",
            "Fetch the latest raw observations from a monitoring adapter.",
            {"adapter": str},
        )
        async def get_adapter_context(args: dict) -> dict:
            ctx = await context_provider(args["adapter"])
            return {"content": [{"type": "text", "text": json.dumps(ctx, default=str)}]}

        @tool(
            "propose_remediation",
            "Record your final decision. Call exactly once. `action` must be one "
            "of the allowed actions, or 'escalate' or 'none'. `severity` is P1, "
            "P2, or P3. `rationale` is a plain-English explanation for the owner.",
            {"action": str, "params": str, "severity": str, "rationale": str},
        )
        async def propose_remediation(args: dict) -> dict:
            proposal.update(args)
            return {"content": [{"type": "text", "text": "recorded"}]}

        server = create_sdk_mcp_server(
            name="slopsaver", version="1.0.0",
            tools=[get_adapter_context, propose_remediation],
        )
        options = ClaudeAgentOptions(
            model=self.MODEL,
            system_prompt=SYSTEM_PROMPT,
            mcp_servers={"slopsaver": server},
            allowed_tools=[
                "mcp__slopsaver__get_adapter_context",
                "mcp__slopsaver__propose_remediation",
            ],
            max_turns=8,
        )

        prompt = (
            f"Anomaly context packet:\n{json.dumps(anomaly.context_packet(), default=str, indent=2)}\n\n"
            f"Allowed remediation actions for adapter '{anomaly.adapter}':\n"
            + "\n".join(f"- {k}: {v}" for k, v in allowed_actions.items())
            + "\n- escalate: hand off to a human with your analysis"
            + "\n- none: false alarm, take no action"
        )

        usage: dict = {}
        cost = duration = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                usage = message.usage or {}
                cost = message.total_cost_usd
                duration = message.duration_ms

        if not proposal:
            raise RuntimeError("agent finished without calling propose_remediation")

        try:
            params = json.loads(proposal.get("params") or "{}")
        except json.JSONDecodeError:
            params = {"raw": proposal.get("params")}

        action = proposal.get("action", "escalate")
        if action not in allowed_actions and action not in ("escalate", "none"):
            action = "escalate"  # never execute an action outside the allowlist

        try:
            severity = Severity(proposal.get("severity", anomaly.severity_hint.value))
        except ValueError:
            severity = anomaly.severity_hint

        decision = RemediationAction(
            anomaly_id=anomaly.id, adapter=anomaly.adapter, action=action,
            params=params, severity=severity,
            rationale=proposal.get("rationale", ""),
        )
        self.audit.llm_call(
            anomaly_id=anomaly.id, model=self.MODEL,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cost_usd=cost, duration_ms=duration,
            decision=decision.model_dump(mode="json"),
        )
        return decision
