"""Fake restaurant auth-service (sandbox). Minimal — exists for topology
realism so the cluster mirrors a real three-service app."""

from __future__ import annotations

import secrets

from fastapi import FastAPI

app = FastAPI(title="fake-restaurant-auth")


@app.post("/token")
async def token():
    return {"access_token": secrets.token_hex(16), "token_type": "bearer"}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
