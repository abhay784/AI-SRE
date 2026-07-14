"""Chaos #1: Stripe webhook silently dropped (charge without order).

Flips the order-service into drop mode, then simulates a Stripe
charge.succeeded webhook that the service will swallow. The reconciliation
adapter should detect the charge/order mismatch on its next poll.

With a real Stripe test-mode account, instead run:
  stripe trigger charge.succeeded   (after enabling drop mode here)
"""

import time

import httpx

from ._common import ORDERS


def main() -> None:
    charge_id = f"ch_chaos_{int(time.time())}"
    with httpx.Client(timeout=10) as client:
        client.post(f"{ORDERS}/admin", json={"drop_webhooks": True})
        print("order-service is now dropping webhooks")
        resp = client.post(
            f"{ORDERS}/webhook/stripe",
            json={
                "id": f"evt_chaos_{charge_id}",
                "type": "charge.succeeded",
                "data": {"object": {"id": charge_id, "payment_intent": f"pi_{charge_id}",
                                    "amount": 4200, "status": "succeeded"}},
            },
        )
        print(f"simulated webhook sent -> {resp.status_code} (order was NOT written)")
    print(f"charge {charge_id} now exists with no matching order.")
    print("undo: python -m sandbox.chaos.drop_stripe_webhook --restore")


def restore() -> None:
    with httpx.Client(timeout=10) as client:
        client.post(f"{ORDERS}/admin", json={"drop_webhooks": False})
    print("order-service webhook processing restored")


if __name__ == "__main__":
    import sys

    restore() if "--restore" in sys.argv else main()
