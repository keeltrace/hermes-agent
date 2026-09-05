from __future__ import annotations

from agent.free_mesh import (
    FreeApiCandidate,
    NominalPricing,
    RuntimeState,
    classify_free,
    resolve_candidate,
)
from agent.free_mesh_sources import apply_free_tier_observations, parse_free_llm_api_hub


def _payload(*, probe_status="live", models_free=None, verified=True):
    return {
        "version": "2.9.0",
        "generated": "2026-09-04",
        "providers": [
            {
                "slug": "wandb-inference",
                "name": "W&B Inference",
                "category": "ongoing",
                "free_type": "recurring-credit",
                "free_tier": "Recurring serverless inference credits",
                "docs_url": "https://example.invalid/wandb",
                "verified": verified,
                "last_verified": "2026-08-14" if verified else None,
                "last_probed": "2026-09-04",
                "probe_status": probe_status,
                "openai_base_url": "https://api.inference.wandb.ai/v1",
                "env_key": "WANDB_API_KEY",
                "models_free": models_free
                if models_free is not None
                else ["Qwen/Qwen3.6-27B"],
            }
        ],
    }


def _candidate(model="Qwen/Qwen3.6-27B", **kwargs):
    defaults = dict(
        provider_id="wandb",
        model_id=model,
        credential_present=True,
        nominal_pricing=NominalPricing(input=0.20, output=0.80),
        runtime=RuntimeState(auth_status="unknown", availability="unknown"),
    )
    defaults.update(kwargs)
    return FreeApiCandidate(**defaults)


def test_verified_live_feed_makes_named_wandb_model_strict_free():
    observations = parse_free_llm_api_hub(_payload())
    enriched = apply_free_tier_observations([_candidate()], observations)[0]

    assert enriched.provider_id == "wandb"
    assert enriched.endpoint == "https://api.inference.wandb.ai/v1"
    assert enriched.free_entitlement.type == "recurring-credit"
    assert enriched.free_entitlement.active is True
    assert enriched.runtime.auth_status == "ok"
    assert enriched.runtime.availability == "available"
    assert enriched.free_confidence >= 0.92
    assert classify_free(enriched) == "free"
    assert resolve_candidate(enriched).eligible is True


def test_sampled_model_list_does_not_mark_unlisted_provider_models_free():
    original = _candidate(model="another/model")
    observations = parse_free_llm_api_hub(_payload())
    enriched = apply_free_tier_observations([original], observations)[0]

    assert enriched.model_id == "another/model"
    assert enriched.free_entitlement.type == "unknown"
    assert classify_free(enriched) == "paid"
    assert resolve_candidate(enriched).exclusion_reason == "runtime-unknown"


def test_tier_ended_changes_entitlement_not_provider_health():
    original = _candidate(
        runtime=RuntimeState(auth_status="ok", availability="available"),
    )
    observations = parse_free_llm_api_hub(_payload(probe_status="tier-ended"))
    enriched = apply_free_tier_observations([original], observations)[0]

    assert enriched.free_entitlement.active is False
    assert enriched.runtime.auth_status == "ok"
    assert enriched.runtime.availability == "available"
    assert classify_free(enriched) == "paid"
    assert resolve_candidate(enriched).exclusion_reason == "incremental-cost-proven"


def test_auth_failed_does_not_reclassify_active_free_entitlement_as_paid():
    observations = parse_free_llm_api_hub(_payload(probe_status="auth-failed"))
    enriched = apply_free_tier_observations([_candidate()], observations)[0]

    assert enriched.free_entitlement.active is True
    assert classify_free(enriched) == "free"
    assert enriched.runtime.auth_status == "failed"
    assert resolve_candidate(enriched).exclusion_reason == "auth-failed"


def test_rate_limited_keeps_free_classification_but_suppresses_execution():
    observations = parse_free_llm_api_hub(_payload(probe_status="rate-limited"))
    enriched = apply_free_tier_observations([_candidate()], observations)[0]

    assert classify_free(enriched) == "free"
    assert enriched.runtime.availability == "rate-limited"
    assert resolve_candidate(enriched).exclusion_reason == "rate-limited"


def test_malformed_feed_is_ignored_safely():
    assert parse_free_llm_api_hub({}) == []
    assert parse_free_llm_api_hub({"providers": "not-a-list"}) == []
    assert parse_free_llm_api_hub({"providers": [{"slug": "x", "free_type": "bogus"}]}) == []
