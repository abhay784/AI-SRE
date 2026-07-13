"""Pluggable adapter interface (PRD §6.4).

Every third-party dependency (Stripe, S3, Postgres, ...) implements this
interface. New integrations plug in without changing the orchestration core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import HealthResult, RemediationResult


class BaseAdapter(ABC):
    """One integration = one adapter.

    Subclasses must define:
      - ``name``: unique adapter id used in config and audit logs
      - ``ACTIONS``: allowlist of remediation actions the reasoning agent may
        propose. Keys are action names; values are one-line descriptions that
        get injected into the agent's prompt. The agent can never invoke an
        action that is not in this dict.
    """

    name: str = ""
    ACTIONS: dict[str, str] = {}
    default_interval: float = 300.0  # seconds between polls unless configured

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    async def check_health(self) -> HealthResult:
        """Poll the integration and return raw observations. Never raises —
        collector failures are reported via HealthResult.error."""

    async def get_context(self) -> dict:
        """Extra context for the reasoning agent's packet. Defaults to the
        latest health observations; adapters override to add history."""
        result = await self.check_health()
        return result.observed

    @abstractmethod
    async def remediate(self, action: str, params: dict) -> RemediationResult:
        """Execute an action from ACTIONS. Must validate `action` membership."""

    def _unknown_action(self, action: str) -> RemediationResult:
        return RemediationResult(
            ok=False,
            detail=f"action {action!r} is not in {self.name}'s allowlist {sorted(self.ACTIONS)}",
        )
