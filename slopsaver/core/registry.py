"""Adapter registry — builds configured adapters from customer config.

Imports are lazy so optional dependencies (stripe, boto3, psycopg, dnspython)
are only required for the integrations a customer actually enables.
"""

from __future__ import annotations

import importlib

from .adapter import BaseAdapter

# adapter name -> "module:Class" within slopsaver.adapters
_ADAPTERS = {
    "stripe": "stripe_adapter:StripeAdapter",
    "postgres": "postgres_adapter:PostgresAdapter",
    "s3": "s3_adapter:S3Adapter",
    "ssl": "ssl_adapter:SSLAdapter",
    "dns": "dns_adapter:DNSAdapter",
    "email": "email_adapter:EmailAdapter",
    "traffic": "traffic_adapter:TrafficAdapter",
    "deploy": "deploy_adapter:DeployAdapter",
    "backup": "backup_adapter:BackupAdapter",
}


def build_adapters(config: dict) -> dict[str, BaseAdapter]:
    """Instantiate every adapter enabled under config['integrations']."""
    adapters: dict[str, BaseAdapter] = {}
    for name, adapter_config in (config.get("integrations") or {}).items():
        if name not in _ADAPTERS:
            raise ValueError(f"unknown integration {name!r}; known: {sorted(_ADAPTERS)}")
        if adapter_config is None or adapter_config.get("enabled", True) is False:
            continue
        module_name, class_name = _ADAPTERS[name].split(":")
        module = importlib.import_module(f"slopsaver.adapters.{module_name}")
        adapters[name] = getattr(module, class_name)(adapter_config)
    return adapters
