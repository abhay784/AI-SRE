"""Fake restaurant api-gateway (sandbox).

Front door of the fake restaurant. Besides serving traffic, it exposes:
  /metrics          — per-IP request rates, form-submission rate, error rate
  /admin            — chaos/remediation switches (block_ip, captcha, email_mode,
                      fail_rate, email metrics overrides, registrar expiry)
  /registrar/domain — fake registrar API (expires_at) for the DNS adapter
  /email/metrics    — fake provider metrics for the email adapter

Chaos scripts flip the same switches the remediation toolkit flips back.
"""

from __future__ import annotations

import os
import random
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="fake-restaurant-gateway")

STATE = {
    "blocked_ips": set(),
    "captcha_required": False,
    "email_mode": "normal",  # normal | throttled | failover
    "fail_rate": float(os.environ.get("FAIL_RATE", "0")),  # bad-deploy chaos
    "email_metrics": {"bounce_rate": 0.01, "complaint_rate": 0.0, "quota_used_pct": 12.0},
    "registrar_expires_at": (datetime.now(timezone.utc) + timedelta(days=200)).isoformat(),
}

# sliding windows
_requests: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=5000))
_form_hits: deque[float] = deque(maxlen=5000)
_errors: deque[float] = deque(maxlen=5000)
_total: deque[float] = deque(maxlen=5000)

WINDOW = 60.0


def _client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", request.client.host if request.client else "?")


def _rate(times: deque[float]) -> float:
    now = time.time()
    return sum(1 for t in times if now - t <= WINDOW) / WINDOW


@app.middleware("http")
async def track(request: Request, call_next):
    ip = _client_ip(request)
    if ip in STATE["blocked_ips"]:
        return JSONResponse({"detail": "blocked"}, status_code=403)
    now = time.time()
    if not request.url.path.startswith(("/metrics", "/admin", "/registrar", "/email")):
        _requests[ip].append(now)
        _total.append(now)
        if random.random() < STATE["fail_rate"]:
            _errors.append(now)
            return JSONResponse({"detail": "internal error (bad deploy)"}, status_code=500)
    return await call_next(request)


# -- customer-facing routes ---------------------------------------------------

@app.get("/")
async def home():
    return {"restaurant": "Slop & Save Diner", "status": "open"}


@app.get("/menu/{item}")
async def menu(item: str):
    return {"item": item, "image": f"s3://menu/{item}.jpg"}


@app.post("/reserve")
async def reserve(request: Request):
    body = await request.json()
    if STATE["captcha_required"] and not body.get("captcha_token"):
        raise HTTPException(status_code=400, detail="captcha required")
    _form_hits.append(time.time())
    return {"reserved": True, "name": body.get("name", "guest")}


# -- monitoring surface ---------------------------------------------------------

@app.get("/metrics")
async def metrics():
    now = time.time()
    total = sum(1 for t in _total if now - t <= WINDOW)
    errors = sum(1 for t in _errors if now - t <= WINDOW)
    return {
        "top_ips": {ip: round(_rate(times), 2) for ip, times in _requests.items() if _rate(times) > 0},
        "form_submissions_per_min": sum(1 for t in _form_hits if now - t <= WINDOW),
        "error_rate": round(errors / total, 3) if total else 0.0,
        "captcha_required": STATE["captcha_required"],
        "email_mode": STATE["email_mode"],
    }


@app.get("/email/metrics")
async def email_metrics():
    return STATE["email_metrics"]


@app.get("/registrar/domain")
async def registrar():
    return {"expires_at": STATE["registrar_expires_at"]}


# -- admin: chaos in, remediation out ---------------------------------------

@app.post("/admin")
async def admin(request: Request):
    body = await request.json()
    if "block_ip" in body:
        STATE["blocked_ips"].add(body["block_ip"])
    if "unblock_all" in body:
        STATE["blocked_ips"].clear()
    if "captcha" in body:
        STATE["captcha_required"] = bool(body["captcha"])
    if "email_mode" in body:
        STATE["email_mode"] = body["email_mode"]
    if "fail_rate" in body:
        STATE["fail_rate"] = float(body["fail_rate"])
    if "email_metrics" in body:
        STATE["email_metrics"].update(body["email_metrics"])
    if "registrar_expires_at" in body:
        STATE["registrar_expires_at"] = body["registrar_expires_at"]
    return {
        "blocked_ips": sorted(STATE["blocked_ips"]),
        "captcha_required": STATE["captcha_required"],
        "email_mode": STATE["email_mode"],
        "fail_rate": STATE["fail_rate"],
    }
