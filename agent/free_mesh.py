"""Free-API intelligence primitives for HermesX.

This module deliberately does *not* route requests.  It answers a narrower
question: for a provider/model offering, what do we currently know about
zero-incremental-cost entitlement and runtime usability?

Routing, ranking, fallback, session affinity, and dispatch remain the job of
OmniRoute/Hermes.  The resolver keeps discovery, entitlement, authentication,
and runtime availability as independent dimensions so a 401/429/provider
failure can never silently turn a free offering into a paid one (or vice versa).

The implementation is local-data-only.  Network-facing source adapters may
feed observations into these dataclasses, but request-time resolution performs
no network I/O.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Optional, Sequence

ApiStyle = Literal["openai", "anthropic", "google", "custom", "unknown"]
AuthStatus = Literal["ok", "failed", "unknown"]
Availability = Literal[
    "available",
    "rate-limited",
    "quota-exhausted",
    "provider-down",
    "unknown",
]
FreeEntitlementType = Literal[
    "always-free",
    "perpetual",
    "renewing-quota",
    "recurring-credit",
    "trial-credit",
    "account-entitlement",
    "unknown",
]
FreeClassification = Literal["free", "paid", "unknown"]


@dataclass(frozen=True)
class EvidenceRecord:
    source: str
    claim: str
    confidence: float = 0.0
    observed_at: Optional[str] = None
    verified: Optional[str] = None


@dataclass(frozen=True)
class CandidateCapabilities:
    text: bool = True
    vision: bool = False
    tools: bool = False
    reasoning: bool = False
    embeddings: bool = False
    audio: bool = False
    context_window: Optional[int] = None


@dataclass(frozen=True)
class NominalPricing:
    """List pricing, independent from the account's current entitlement."""

    input: Optional[float] = None
    output: Optional[float] = None
    cache_read: Optional[float] = None
    cache_write: Optional[float] = None
    other_possible_charges: bool = False

    def observed_values(self) -> tuple[float, ...]:
        values = (self.input, self.output, self.cache_read, self.cache_write)
        return tuple(float(value) for value in values if value is not None)

    @property
    def proven_nonzero(self) -> bool:
        return any(value > 0 for value in self.observed_values())

    @property
    def proven_zero(self) -> bool:
        # Zero token prices alone are insufficient when another charge dimension
        # may exist.  Also require at least input+output observations so an empty
        # pricing record cannot masquerade as free.
        if self.input is None or self.output is None or self.other_possible_charges:
            return False
        return all(value == 0 for value in self.observed_values())


@dataclass(frozen=True)
class FreeEntitlement:
    type: FreeEntitlementType = "unknown"
    active: bool = False
    amount: Optional[float] = None
    unit: Optional[str] = None
    reset_at: Optional[str] = None
    source: Optional[str] = None


@dataclass(frozen=True)
class RuntimeState:
    auth_status: AuthStatus = "unknown"
    availability: Availability = "unknown"
    last_successful_request: Optional[str] = None
    last_probe_at: Optional[str] = None
    remaining_allowance: Optional[float] = None


@dataclass(frozen=True)
class FreeApiCandidate:
    provider_id: str
    model_id: str
    provider_aliases: tuple[str, ...] = ()
    model_aliases: tuple[str, ...] = ()
    endpoint: Optional[str] = None
    api_style: ApiStyle = "unknown"
    credential_present: bool = False
    capabilities: CandidateCapabilities = field(default_factory=CandidateCapabilities)
    nominal_pricing: NominalPricing = field(default_factory=NominalPricing)
    free_entitlement: FreeEntitlement = field(default_factory=FreeEntitlement)
    runtime: RuntimeState = field(default_factory=RuntimeState)
    evidence: tuple[EvidenceRecord, ...] = ()
    free_confidence: float = 0.0
    discovery_confidence: float = 0.0
    discovery_stale: bool = False

    def canonicalized(self) -> "FreeApiCandidate":
        canonical = canonical_provider_id(self.provider_id)
        aliases = tuple(dict.fromkeys((*self.provider_aliases, self.provider_id)))
        return _replace_candidate(self, provider_id=canonical, provider_aliases=aliases)


@dataclass(frozen=True)
class ResolvedFreeCandidate:
    candidate: FreeApiCandidate
    free_classification: FreeClassification
    incremental_cost_now: Optional[float]
    eligible: bool
    exclusion_reason: Optional[str]
    free_confidence: float


_PROVIDER_ALIASES = {
    "nous": "nous-research",
    "nousresearch": "nous-research",
    "nous_research": "nous-research",
    "nous-research": "nous-research",
    "wandb": "wandb",
    "weights-and-biases": "wandb",
    "weights_biases": "wandb",
    "wandb-inference": "wandb",
}

# Operator policy outranks third-party metadata.  This is intentionally tiny:
# policies should be explicit, auditable exceptions rather than a second
# hard-coded catalog.
_ALWAYS_FREE_PROVIDERS = frozenset({"nous-research"})


def canonical_provider_id(provider_id: str) -> str:
    raw = str(provider_id or "").strip().lower()
    if not raw:
        return "unknown"
    normalized = raw.replace(" ", "-")
    return _PROVIDER_ALIASES.get(normalized, normalized)


def is_provider_always_free(provider_id: str) -> bool:
    return canonical_provider_id(provider_id) in _ALWAYS_FREE_PROVIDERS


def _clamp_confidence(value: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _replace_candidate(candidate: FreeApiCandidate, **updates) -> FreeApiCandidate:
    data = {
        "provider_id": candidate.provider_id,
        "model_id": candidate.model_id,
        "provider_aliases": candidate.provider_aliases,
        "model_aliases": candidate.model_aliases,
        "endpoint": candidate.endpoint,
        "api_style": candidate.api_style,
        "credential_present": candidate.credential_present,
        "capabilities": candidate.capabilities,
        "nominal_pricing": candidate.nominal_pricing,
        "free_entitlement": candidate.free_entitlement,
        "runtime": candidate.runtime,
        "evidence": candidate.evidence,
        "free_confidence": candidate.free_confidence,
        "discovery_confidence": candidate.discovery_confidence,
        "discovery_stale": candidate.discovery_stale,
    }
    data.update(updates)
    return FreeApiCandidate(**data)


def _policy_evidence(candidate: FreeApiCandidate) -> tuple[EvidenceRecord, ...]:
    if not is_provider_always_free(candidate.provider_id):
        return ()
    return (
        EvidenceRecord(
            source="operator-policy",
            claim="provider is explicitly classified always-free",
            confidence=0.99,
        ),
    )


def classify_free(candidate: FreeApiCandidate) -> FreeClassification:
    """Classify entitlement without considering temporary runtime availability."""

    candidate = candidate.canonicalized()
    if is_provider_always_free(candidate.provider_id):
        return "free"

    entitlement = candidate.free_entitlement
    if entitlement.active and entitlement.type != "unknown":
        return "free"

    # Provider-specific free SKU markers are direct product semantics, not a
    # transient runtime observation.  They are still weaker than account proof.
    if candidate.model_id.endswith(":free"):
        return "free"

    if candidate.nominal_pricing.proven_nonzero:
        return "paid"
    if candidate.nominal_pricing.proven_zero:
        return "free"
    return "unknown"


def effective_free_confidence(candidate: FreeApiCandidate) -> float:
    candidate = candidate.canonicalized()
    confidence = _clamp_confidence(candidate.free_confidence)
    for evidence in (*candidate.evidence, *_policy_evidence(candidate)):
        confidence = max(confidence, _clamp_confidence(evidence.confidence))
    if candidate.free_entitlement.active and candidate.free_entitlement.type != "unknown":
        confidence = max(confidence, 0.95)
    if candidate.model_id.endswith(":free"):
        confidence = max(confidence, 0.90)
    if candidate.nominal_pricing.proven_zero:
        confidence = max(confidence, 0.80)
    return confidence


def incremental_cost_now(candidate: FreeApiCandidate) -> Optional[float]:
    classification = classify_free(candidate)
    if classification == "free":
        return 0.0
    if classification == "paid" and candidate.nominal_pricing.proven_nonzero:
        # Exact per-request cost depends on usage; a positive sentinel preserves
        # the only fact strict-free mode needs: incremental spend is possible.
        return min(value for value in candidate.nominal_pricing.observed_values() if value > 0)
    return None


def resolve_candidate(
    candidate: FreeApiCandidate,
    *,
    strict_zero_cost: bool = True,
    min_free_confidence: float = 0.80,
) -> ResolvedFreeCandidate:
    """Resolve one provider/model offering into an execution eligibility fact.

    UNKNOWN is deliberately neither paid nor safe-free.  Runtime failures only
    affect availability/authentication; they never rewrite free classification.
    """

    candidate = candidate.canonicalized()
    classification = classify_free(candidate)
    confidence = effective_free_confidence(candidate)
    cost_now = incremental_cost_now(candidate)

    reason: Optional[str] = None
    if not candidate.credential_present:
        reason = "credential-missing"
    elif candidate.runtime.auth_status == "failed":
        reason = "auth-failed"
    elif candidate.runtime.availability == "rate-limited":
        reason = "rate-limited"
    elif candidate.runtime.availability == "quota-exhausted":
        reason = "quota-exhausted"
    elif candidate.runtime.availability == "provider-down":
        reason = "provider-down"
    elif candidate.runtime.availability != "available":
        reason = "runtime-unknown"
    elif strict_zero_cost and classification == "paid":
        reason = "incremental-cost-proven"
    elif strict_zero_cost and classification == "unknown":
        reason = "incremental-cost-unknown"
    elif strict_zero_cost and confidence < min_free_confidence:
        reason = "free-confidence-too-low"

    return ResolvedFreeCandidate(
        candidate=candidate,
        free_classification=classification,
        incremental_cost_now=cost_now,
        eligible=reason is None,
        exclusion_reason=reason,
        free_confidence=confidence,
    )


def resolve_free_candidates(
    candidates: Iterable[FreeApiCandidate],
    *,
    strict_zero_cost: bool = True,
    min_free_confidence: float = 0.80,
) -> list[ResolvedFreeCandidate]:
    """Return eligible candidates only; ordering is intentionally unchanged.

    This function does not rank.  Callers hand the resulting pool to the router.
    """

    resolved = [
        resolve_candidate(
            candidate,
            strict_zero_cost=strict_zero_cost,
            min_free_confidence=min_free_confidence,
        )
        for candidate in candidates
    ]
    return [item for item in resolved if item.eligible]


def apply_http_status(candidate: FreeApiCandidate, status_code: int) -> FreeApiCandidate:
    """Apply runtime evidence without mutating the candidate's free entitlement."""

    runtime = candidate.runtime
    auth_status = runtime.auth_status
    availability = runtime.availability

    if status_code == 401:
        auth_status = "failed"
    elif status_code == 429:
        availability = "rate-limited"
    elif 500 <= status_code <= 599:
        availability = "provider-down"
    elif 200 <= status_code <= 299:
        auth_status = "ok"
        availability = "available"

    return _replace_candidate(
        candidate,
        runtime=RuntimeState(
            auth_status=auth_status,
            availability=availability,
            last_successful_request=runtime.last_successful_request,
            last_probe_at=runtime.last_probe_at,
            remaining_allowance=runtime.remaining_allowance,
        ),
    )


def retain_last_known_good(
    previous: Sequence[FreeApiCandidate],
    fresh: Sequence[FreeApiCandidate],
    *,
    refresh_succeeded: bool,
) -> list[FreeApiCandidate]:
    """Stale-while-revalidate inventory merge.

    A failed discovery refresh never erases the previous model list.  The
    retained records are marked discovery_stale so ranking/diagnostics can
    prefer fresh evidence without turning a transient `/models` failure into an
    empty catalog.
    """

    if refresh_succeeded:
        return list(fresh)
    return [_replace_candidate(candidate, discovery_stale=True) for candidate in previous]


class FreeCandidateStore:
    """Small SQLite last-known-good store for normalized candidates.

    It stores resolved observations, not routing choices.  The schema is
    intentionally self-contained so Hermes can boot from the last known good
    state even when every external intelligence source is unavailable.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS free_candidates (
                    provider_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (provider_id, model_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_sync_state (
                    source TEXT PRIMARY KEY,
                    last_success_at REAL,
                    last_failure_at REAL,
                    last_error TEXT
                )
                """
            )

    def replace_candidates(self, candidates: Iterable[FreeApiCandidate]) -> None:
        rows = []
        now = time.time()
        for candidate in candidates:
            canonical = candidate.canonicalized()
            rows.append(
                (
                    canonical.provider_id,
                    canonical.model_id,
                    json.dumps(asdict(canonical), sort_keys=True, separators=(",", ":")),
                    now,
                )
            )
        with self._connect() as conn:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM free_candidates")
            conn.executemany(
                "INSERT INTO free_candidates(provider_id, model_id, payload, updated_at) VALUES(?,?,?,?)",
                rows,
            )

    def load_candidates(self) -> list[FreeApiCandidate]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM free_candidates ORDER BY provider_id, model_id"
            ).fetchall()
        return [_candidate_from_dict(json.loads(payload)) for (payload,) in rows]

    def record_source_result(self, source: str, *, success: bool, error: Optional[str] = None) -> None:
        now = time.time()
        with self._connect() as conn:
            prior = conn.execute(
                "SELECT last_success_at, last_failure_at FROM source_sync_state WHERE source=?",
                (source,),
            ).fetchone()
            last_success = prior[0] if prior else None
            last_failure = prior[1] if prior else None
            if success:
                last_success = now
                error = None
            else:
                last_failure = now
            conn.execute(
                """
                INSERT INTO source_sync_state(source, last_success_at, last_failure_at, last_error)
                VALUES(?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET
                    last_success_at=excluded.last_success_at,
                    last_failure_at=excluded.last_failure_at,
                    last_error=excluded.last_error
                """,
                (source, last_success, last_failure, error),
            )


def _candidate_from_dict(data: dict) -> FreeApiCandidate:
    capabilities = CandidateCapabilities(**data.get("capabilities", {}))
    pricing = NominalPricing(**data.get("nominal_pricing", {}))
    entitlement = FreeEntitlement(**data.get("free_entitlement", {}))
    runtime = RuntimeState(**data.get("runtime", {}))
    evidence = tuple(EvidenceRecord(**item) for item in data.get("evidence", ()))
    return FreeApiCandidate(
        provider_id=data["provider_id"],
        model_id=data["model_id"],
        provider_aliases=tuple(data.get("provider_aliases", ())),
        model_aliases=tuple(data.get("model_aliases", ())),
        endpoint=data.get("endpoint"),
        api_style=data.get("api_style", "unknown"),
        credential_present=bool(data.get("credential_present", False)),
        capabilities=capabilities,
        nominal_pricing=pricing,
        free_entitlement=entitlement,
        runtime=runtime,
        evidence=evidence,
        free_confidence=float(data.get("free_confidence", 0.0)),
        discovery_confidence=float(data.get("discovery_confidence", 0.0)),
        discovery_stale=bool(data.get("discovery_stale", False)),
    )
