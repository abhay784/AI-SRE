"""Stripe reconciliation adapter (PRD failure #1).

Detection: list recent charges via a restricted read-only key and reconcile
against the app's orders table. A charge with no matching order means a
webhook was dropped — the customer paid but the kitchen never saw the order.

Remediation: replay the missed webhook event to the app's webhook endpoint,
or reconstruct the order directly from the charge object.

Config:
  api_key_env: env var holding the restricted Stripe key (default STRIPE_API_KEY)
  orders_dsn: postgres DSN for the app's orders table (read-only role)
  orders_table: table name (default "orders")
  webhook_url: the app's Stripe webhook endpoint, for replays
  lookback_minutes: reconciliation window (default 60)
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import httpx

from ..core.adapter import BaseAdapter
from ..core.models import HealthResult, RemediationResult


class StripeAdapter(BaseAdapter):
    name = "stripe"
    default_interval = 300.0  # every 5 minutes per PRD §7
    ACTIONS = {
        "replay_webhook": (
            "Re-send the missed Stripe event to the app's webhook endpoint. "
            "Required param: charge_id (string) — copy it verbatim from the "
            "anomaly context's charge_id field."
        ),
        "reconstruct_order": (
            "Rebuild the order row directly from the Stripe charge object. "
            "Required param: charge_id (string) — copy it verbatim from the "
            "anomaly context's charge_id field."
        ),
    }

    def _client(self):
        import stripe

        stripe.api_key = os.environ[self.config.get("api_key_env", "STRIPE_API_KEY")]
        return stripe

    # -- detection ------------------------------------------------------------

    async def check_health(self) -> HealthResult:
        try:
            return await asyncio.to_thread(self._reconcile)
        except Exception as exc:
            return HealthResult(adapter=self.name, ok=False, error=str(exc))

    def _reconcile(self) -> HealthResult:
        stripe = self._client()
        lookback = int(self.config.get("lookback_minutes", 60))
        since = int((datetime.now(timezone.utc) - timedelta(minutes=lookback)).timestamp())

        charges = stripe.Charge.list(created={"gte": since}, limit=100).data
        order_keys = self._fetch_order_keys(since)

        unmatched = [
            {"id": c.id, "amount": c.amount, "payment_intent": c.payment_intent,
             "created": c.created}
            for c in charges
            if c.status == "succeeded" and not self._charge_matches_order(c, order_keys)
        ]
        webhook_errors = self._recent_webhook_errors(stripe, since)
        return HealthResult(
            adapter=self.name,
            ok=not unmatched and len(webhook_errors) < 3,
            observed={
                "charges_checked": len(charges),
                "unmatched_charges": unmatched,
                "webhook_error_count": len(webhook_errors),
                "recent_webhook_errors": webhook_errors[:5],
            },
        )

    def _charge_matches_order(self, charge, order_keys: set[str]) -> bool:
        """Decide whether a Stripe charge corresponds to a known order.

        TODO(user): this matching rule is the business-logic heart of the
        Stripe adapter and depends on how the app writes orders. The default
        below matches on payment_intent id, falling back to charge id. Other
        valid designs: match on (amount, customer, ±2min window) for apps that
        don't store payment_intent; or require order.status != 'cancelled'.
        Tighten this to your schema — a too-loose rule misses real losses, a
        too-strict one pages the owner with false alarms.
        """
        return (charge.payment_intent or charge.id) in order_keys

    def _fetch_order_keys(self, since_epoch: int) -> set[str]:
        import psycopg

        table = self.config.get("orders_table", "orders")
        with psycopg.connect(self.config["orders_dsn"]) as conn:
            rows = conn.execute(
                f"SELECT payment_ref FROM {table} WHERE created_at >= to_timestamp(%s)",
                (since_epoch,),
            ).fetchall()
        return {r[0] for r in rows if r[0]}

    def _recent_webhook_errors(self, stripe, since_epoch: int) -> list[str]:
        events = stripe.Event.list(created={"gte": since_epoch}, limit=100,
                                   delivery_success=False).data
        return [f"{e.type} ({e.id})" for e in events]

    # -- remediation ----------------------------------------------------------

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        if action not in self.ACTIONS:
            return self._unknown_action(action)
        try:
            if action == "replay_webhook":
                return await self._replay_webhook(params)
            return await asyncio.to_thread(self._reconstruct_order, params)
        except Exception as exc:
            return RemediationResult(ok=False, detail=str(exc))

    async def _replay_webhook(self, params: dict) -> RemediationResult:
        charge_id = params.get("charge_id") or params.get("id")
        if not charge_id:
            return RemediationResult(ok=False, detail="replay_webhook requires charge_id")
        stripe = self._client()
        charge = await asyncio.to_thread(stripe.Charge.retrieve, charge_id)
        event_body = {
            "id": f"evt_replay_{charge_id}",
            "type": "charge.succeeded",
            "data": {"object": json.loads(json.dumps(charge, default=str))
                     if not hasattr(charge, "to_dict") else charge.to_dict()},
            "replayed_by": "slopsaver",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self.config["webhook_url"], json=event_body)
        ok = resp.status_code < 300
        return RemediationResult(ok=ok, detail=f"webhook replay returned {resp.status_code}")

    def _reconstruct_order(self, params: dict) -> RemediationResult:
        import psycopg

        charge_id = params.get("charge_id") or params.get("id")
        if not charge_id:
            return RemediationResult(ok=False, detail="reconstruct_order requires charge_id")
        stripe = self._client()
        charge = stripe.Charge.retrieve(charge_id)
        table = self.config.get("orders_table", "orders")
        with psycopg.connect(self.config["orders_dsn"]) as conn:
            conn.execute(
                f"INSERT INTO {table} (payment_ref, amount, status, created_at) "
                "VALUES (%s, %s, 'reconstructed', now()) ON CONFLICT DO NOTHING",
                (charge.payment_intent or charge.id, charge.amount),
            )
            conn.commit()
        return RemediationResult(ok=True, detail=f"order reconstructed from {charge_id}")
