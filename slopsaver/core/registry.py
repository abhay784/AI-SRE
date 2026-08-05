"""Adapter registry — builds configured adapters from customer config.

Imports are lazy so optional dependencies (stripe, boto3, psycopg, dnspython)
are only required for the integrations a customer actually enables.
"""

from __future__ import annotations

import importlib
import math

from .adapter import BaseAdapter

# adapter name -> "module:Class" within slopsaver.adapters
_ADAPTERS = {
    "website": "website_adapter:WebsiteAdapter",
    "github": "github_adapter:GitHubAdapter",
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
    integrations = config.get("integrations") or {}
    if not isinstance(integrations, dict):
        raise ValueError("integrations must be a YAML mapping")
    for name, adapter_config in integrations.items():
        if name not in _ADAPTERS:
            raise ValueError(f"unknown integration {name!r}; known: {sorted(_ADAPTERS)}")
        if adapter_config is None:
            continue
        if not isinstance(adapter_config, dict):
            raise ValueError(f"{name} configuration must be a YAML mapping")
        if adapter_config.get("enabled", True) is False:
            continue
        interval = float(adapter_config.get("interval", 60))
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError(f"{name}.interval must be a positive finite number")
        module_name, class_name = _ADAPTERS[name].split(":")
        module = importlib.import_module(f"slopsaver.adapters.{module_name}")
        adapters[name] = getattr(module, class_name)(adapter_config)
    if not adapters:
        raise ValueError("configure at least one enabled integration")
    return adapters
