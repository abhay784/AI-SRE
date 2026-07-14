"""Shared endpoints for chaos scripts (kind NodePort defaults)."""

import os

GATEWAY = os.environ.get("SANDBOX_GATEWAY", "http://localhost:30080")
ORDERS = os.environ.get("SANDBOX_ORDERS", "http://localhost:30081")
POSTGRES_DSN = os.environ.get(
    "SANDBOX_POSTGRES", "postgresql://restaurant:restaurant@localhost:30432/restaurant"
)
MINIO_ENDPOINT = os.environ.get("SANDBOX_MINIO", "http://localhost:30900")
MINIO_KEY = os.environ.get("MINIO_ROOT_USER", "slopsaver")
MINIO_SECRET = os.environ.get("MINIO_ROOT_PASSWORD", "slopsaver123")
