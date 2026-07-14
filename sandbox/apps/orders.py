"""Fake restaurant order-service (sandbox).

Receives Stripe webhooks and writes orders to Postgres. The chaos switch
`drop_webhooks` makes it silently ignore incoming events — the exact failure
mode the Stripe reconciliation adapter exists to catch.

Env:
  DATABASE_URL: postgres DSN (default matches the sandbox manifest)
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://restaurant:restaurant@postgres:5432/restaurant"
)

app = FastAPI(title="fake-restaurant-orders")
STATE = {"drop_webhooks": False}


def _conn():
    import psycopg

    return psycopg.connect(DSN)


@app.on_event("startup")
def ensure_schema():
    try:
        with _conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS orders ("
                "  id serial PRIMARY KEY,"
                "  payment_ref text UNIQUE,"
                "  amount integer,"
                "  status text DEFAULT 'received',"
                "  created_at timestamptz DEFAULT now()"
                ")"
            )
            conn.commit()
    except Exception as exc:  # pragma: no cover - postgres may lag on startup
        print(f"schema init deferred: {exc}")


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    event = await request.json()
    if STATE["drop_webhooks"] and not event.get("replayed_by"):
        # Chaos mode: pretend we processed it (Stripe sees 200, order is lost).
        return {"received": True}
    obj = event.get("data", {}).get("object", {})
    payment_ref = obj.get("payment_intent") or obj.get("id")
    if payment_ref:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO orders (payment_ref, amount) VALUES (%s, %s) "
                "ON CONFLICT (payment_ref) DO NOTHING",
                (payment_ref, obj.get("amount", 0)),
            )
            conn.commit()
    return {"received": True, "payment_ref": payment_ref}


@app.get("/orders")
async def list_orders():
    with _conn() as conn:
        rows = conn.execute(
            "SELECT payment_ref, amount, status, created_at FROM orders "
            "ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    return [
        {"payment_ref": r[0], "amount": r[1], "status": r[2], "created_at": str(r[3])}
        for r in rows
    ]


@app.post("/admin")
async def admin(request: Request):
    body = await request.json()
    if "drop_webhooks" in body:
        STATE["drop_webhooks"] = bool(body["drop_webhooks"])
    return STATE
