"""Regression coverage for the params-backfill bug.

Found live against the real Claude Agent SDK: when asked to propose
block_ip / replay_webhook / reconstruct_order, the model would send `params`
with unrelated diagnostic fields instead of the actual key the adapter
requires (ip / charge_id), because neither the adapter's ACTIONS description
nor the propose_remediation tool prompt said what params should contain.
`FakeReasoner` had the identical gap for a different reason (it never
threaded anomaly.context into params at all).

backfill_required_params() is the shared fix: whatever supplied params (a
table lookup or an LLM), fill in any key the action is known to need from the
anomaly context, without overriding a value already present.
"""

import importlib
import pkgutil

from ai_sre.core.models import AnomalyEvent, Severity
from ai_sre.core.reasoning import (
    ACTION_PARAM_SOURCES,
    FakeReasoner,
    backfill_required_params,
)

TRAFFIC_ACTIONS = {"block_ip": "Block a single IP (param: ip)", "enable_captcha": "captcha"}
STRIPE_ACTIONS = {"replay_webhook": "replay (param: charge_id)"}


def _anomaly(adapter: str, anomaly_type: str, context: dict) -> AnomalyEvent:
    return AnomalyEvent(adapter=adapter, anomaly_type=anomaly_type,
                        severity_hint=Severity.P3, context=context)


def test_backfill_fills_missing_required_key():
    anomaly = _anomaly("traffic", "endpoint_abuse", {"ip": "203.0.113.66", "rps": 15.0})
    params = backfill_required_params("block_ip", {}, anomaly)
    assert params == {"ip": "203.0.113.66"}


def test_backfill_never_overrides_a_supplied_value():
    """If the model legitimately supplied its own value, trust it — the
    backfill is a safety net for omissions, not a source of truth."""
    anomaly = _anomaly("traffic", "endpoint_abuse", {"ip": "203.0.113.66"})
    params = backfill_required_params("block_ip", {"ip": "198.51.100.1"}, anomaly)
    assert params == {"ip": "198.51.100.1"}


def test_backfill_is_noop_for_actions_with_no_required_params():
    anomaly = _anomaly("s3", "s3_bucket_public", {"public_access": True})
    assert backfill_required_params("revert_policy", {}, anomaly) == {}


def test_backfill_leaves_gap_when_context_lacks_the_key():
    """Can't fill what isn't there — this documents the residual risk rather
    than hiding it: an action still fails loudly if the anomaly itself never
    carried the needed field, instead of silently substituting garbage."""
    anomaly = _anomaly("traffic", "endpoint_abuse", {})
    assert backfill_required_params("block_ip", {}, anomaly) == {}


async def test_fake_reasoner_threads_ip_into_block_ip():
    anomaly = _anomaly("traffic", "endpoint_abuse", {"ip": "203.0.113.66", "rps": 15.0})
    decision = await FakeReasoner().decide(anomaly, TRAFFIC_ACTIONS)
    assert decision.action == "block_ip"
    assert decision.params == {"ip": "203.0.113.66"}


async def test_fake_reasoner_threads_charge_id_into_replay_webhook():
    anomaly = _anomaly("stripe", "stripe_order_mismatch", {"charge_id": "ch_42", "amount": 4200})
    decision = await FakeReasoner().decide(anomaly, STRIPE_ACTIONS)
    assert decision.action == "replay_webhook"
    assert decision.params == {"charge_id": "ch_42"}


# -- keep ACTIONS text honest: every action with a required param source
# must say so, so a human editing an adapter can't silently drop the
# documentation the reasoning agent depends on. ----------------------------

def _all_adapter_classes():
    import ai_sre.adapters as adapters_pkg
    from ai_sre.core.adapter import BaseAdapter

    classes = []
    for _, module_name, _ in pkgutil.iter_modules(adapters_pkg.__path__):
        module = importlib.import_module(f"ai_sre.adapters.{module_name}")
        for attr in vars(module).values():
            if (isinstance(attr, type) and issubclass(attr, BaseAdapter)
                    and attr is not BaseAdapter and attr.__module__ == module.__name__):
                classes.append(attr)
    return classes


REQUIRED_PARAM_HINT = {
    "ip": "ip",
    "charge_id": "charge_id",
}


def test_actions_with_required_params_document_the_key_name():
    for adapter_cls in _all_adapter_classes():
        for action, required_keys in ACTION_PARAM_SOURCES.items():
            if action not in adapter_cls.ACTIONS:
                continue
            description = adapter_cls.ACTIONS[action]
            for key in required_keys:
                hint = REQUIRED_PARAM_HINT[key]
                assert hint in description, (
                    f"{adapter_cls.__name__}.ACTIONS[{action!r}] must mention "
                    f"'{hint}' so the reasoning agent knows what to send: {description!r}"
                )


def test_actions_without_required_params_say_so():
    """Actions the reasoning agent might pick that need no params should say
    so explicitly ('No params required') rather than leaving it implicit —
    an ambiguous description is exactly what caused this bug."""
    for adapter_cls in _all_adapter_classes():
        for action, description in adapter_cls.ACTIONS.items():
            if action in ACTION_PARAM_SOURCES:
                continue
            assert "param" in description.lower(), (
                f"{adapter_cls.__name__}.ACTIONS[{action!r}] should state its params "
                f"requirement explicitly (even 'no params required'): {description!r}"
            )
