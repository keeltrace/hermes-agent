"""Source adapters for the HermesX free-API intelligence mesh.

Adapters in this module are deliberately pure parsers: raw upstream snapshot ->
normalized observations. They never route, dispatch, or perform network I/O.
The caller owns refresh cadence, persistence, and stale-while-revalidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from agent.free_mesh import (
    EvidenceRecord,
    FreeApiCandidate,
    FreeEntitlement,
    RuntimeState,
    canonical_provider_id,
)


@dataclass(frozen=True)
class ProviderFreeTierObservation:
    provider_id: str
    entitlement_type: str
    verified: bool
    last_verified: Optional[str]
    last_probed: Optional[str]
    probe_status: Optional[str]
    endpoint: Optional[str]
    env_key: Optional[str]
    models_free: tuple[str, ...]
    evidence: tuple[EvidenceRecord, ...]


_FREE_TYPES = {
    "perpetual",
    "renewing-quota",
    "recurring-credit",
    "trial-credit",
}

_PROBE_STATUSES = {
    "live",
    "auth-ok",
    "auth-failed",
    "tier-ended",
    "rate-limited",
    "error",
}


def parse_free_llm_api_hub(payload: Mapping[str, Any]) -> list[ProviderFreeTierObservation]:
    """Parse free-llm-api-hub ``data/providers.json`` into normalized observations.

    The upstream schema explicitly treats ``models_free`` as a sampled attribute,
    so this adapter never assumes that a provider-level free program makes every
    discovered model free. Only model IDs named by the feed are enriched below.
    """

    providers = payload.get("providers")
    if not isinstance(providers, list):
        return []

    observations: list[ProviderFreeTierObservation] = []
    for raw in providers:
        if not isinstance(raw, Mapping):
            continue
        slug = raw.get("slug")
        free_type = raw.get("free_type")
        if not isinstance(slug, str) or not slug.strip():
            continue
        if free_type not in _FREE_TYPES:
            continue

        verified = raw.get("verified") is True
        last_verified = raw.get("last_verified") if isinstance(raw.get("last_verified"), str) else None
        last_probed = raw.get("last_probed") if isinstance(raw.get("last_probed"), str) else None
        probe_status = raw.get("probe_status")
        if probe_status not in _PROBE_STATUSES:
            probe_status = None
        endpoint = raw.get("openai_base_url") if isinstance(raw.get("openai_base_url"), str) else None
        env_key = raw.get("env_key") if isinstance(raw.get("env_key"), str) else None
        models_raw = raw.get("models_free")
        models_free = tuple(
            dict.fromkeys(
                model.strip()
                for model in models_raw
                if isinstance(model, str) and model.strip()
            )
        ) if isinstance(models_raw, list) else ()

        confidence = 0.80 if verified else 0.55
        if probe_status == "live":
            confidence = max(confidence, 0.92)
        elif probe_status == "auth-ok":
            confidence = max(confidence, 0.82)
        elif probe_status == "tier-ended":
            confidence = max(confidence, 0.90)

        claims = [
            EvidenceRecord(
                source="free-llm-api-hub",
                claim=f"provider free program classified as {free_type}",
                confidence=confidence,
                verified=last_verified,
            )
        ]
        if probe_status:
            claims.append(
                EvidenceRecord(
                    source="free-llm-api-hub:probe",
                    claim=f"latest probe status: {probe_status}",
                    confidence=confidence,
                    observed_at=last_probed,
                )
            )

        observations.append(
            ProviderFreeTierObservation(
                provider_id=canonical_provider_id(slug),
                entitlement_type=free_type,
                verified=verified,
                last_verified=last_verified,
                last_probed=last_probed,
                probe_status=probe_status,
                endpoint=endpoint,
                env_key=env_key,
                models_free=models_free,
                evidence=tuple(claims),
            )
        )

    return observations


def _runtime_from_probe(existing: RuntimeState, probe_status: Optional[str]) -> RuntimeState:
    auth = existing.auth_status
    availability = existing.availability

    if probe_status == "live":
        auth = "ok"
        availability = "available"
    elif probe_status == "auth-ok":
        auth = "ok"
    elif probe_status == "auth-failed":
        auth = "failed"
    elif probe_status == "rate-limited":
        auth = "ok"
        availability = "rate-limited"
    # tier-ended is an entitlement observation, not a provider-health state.
    # error is ambiguous (transport/provider/probe failure), so neither is
    # allowed to overwrite the candidate's runtime availability here.

    return RuntimeState(
        auth_status=auth,
        availability=availability,
        last_successful_request=existing.last_successful_request,
        last_probe_at=existing.last_probe_at,
        remaining_allowance=existing.remaining_allowance,
    )


def apply_free_tier_observations(
    candidates: Iterable[FreeApiCandidate],
    observations: Iterable[ProviderFreeTierObservation],
) -> list[FreeApiCandidate]:
    """Enrich model candidates using provider/model claims from a free-tier feed.

    Matching is provider + exact model ID. A provider observation with an empty
    ``models_free`` list contributes no model-level entitlement because the
    upstream schema says that field is sampled and may be incomplete.
    """

    by_provider = {item.provider_id: item for item in observations}
    enriched: list[FreeApiCandidate] = []

    for original in candidates:
        candidate = original.canonicalized()
        observation = by_provider.get(candidate.provider_id)
        if observation is None or candidate.model_id not in observation.models_free:
            enriched.append(candidate)
            continue

        active = observation.probe_status != "tier-ended"
        entitlement = FreeEntitlement(
            type=observation.entitlement_type,  # type: ignore[arg-type]
            active=active,
            source="free-llm-api-hub",
        )
        runtime = _runtime_from_probe(candidate.runtime, observation.probe_status)
        free_confidence = max(
            candidate.free_confidence,
            *(evidence.confidence for evidence in observation.evidence),
        )

        enriched.append(
            FreeApiCandidate(
                provider_id=candidate.provider_id,
                model_id=candidate.model_id,
                provider_aliases=candidate.provider_aliases,
                model_aliases=candidate.model_aliases,
                endpoint=candidate.endpoint or observation.endpoint,
                api_style=candidate.api_style,
                credential_present=candidate.credential_present,
                capabilities=candidate.capabilities,
                nominal_pricing=candidate.nominal_pricing,
                free_entitlement=entitlement,
                runtime=runtime,
                evidence=tuple((*candidate.evidence, *observation.evidence)),
                free_confidence=free_confidence,
                discovery_confidence=candidate.discovery_confidence,
                discovery_stale=candidate.discovery_stale,
            )
        )

    return enriched
