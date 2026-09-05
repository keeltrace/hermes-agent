from __future__ import annotations

from agent.free_mesh import (
    CandidateCapabilities,
    EvidenceRecord,
    FreeApiCandidate,
    FreeCandidateStore,
    FreeEntitlement,
    NominalPricing,
    RuntimeState,
    apply_http_status,
    classify_free,
    resolve_candidate,
    retain_last_known_good,
)


def _candidate(**kwargs):
    defaults = dict(
        provider_id="groq",
        model_id="example/model",
        credential_present=True,
        runtime=RuntimeState(auth_status="ok", availability="available"),
    )
    defaults.update(kwargs)
    return FreeApiCandidate(**defaults)


def test_nous_policy_is_free_even_without_numeric_credit_proof():
    candidate = _candidate(
        provider_id="nous",
        model_id="Hermes-4-405B",
        nominal_pricing=NominalPricing(input=3.0, output=15.0),
        free_entitlement=FreeEntitlement(type="unknown", active=False),
    )
    resolved = resolve_candidate(candidate)
    assert classify_free(candidate) == "free"
    assert resolved.eligible is True
    assert resolved.incremental_cost_now == 0.0
    assert resolved.free_confidence >= 0.99


def test_wandb_account_entitlement_overrides_nonzero_nominal_pricing():
    candidate = _candidate(
        provider_id="wandb-inference",
        model_id="Qwen/Qwen3.6-27B",
        nominal_pricing=NominalPricing(input=0.20, output=0.80),
        free_entitlement=FreeEntitlement(
            type="account-entitlement",
            active=True,
            amount=5.0,
            unit="USD",
            source="account-probe",
        ),
        evidence=(EvidenceRecord("account-probe", "free credit remaining", 0.98),),
    )
    resolved = resolve_candidate(candidate)
    assert resolved.free_classification == "free"
    assert resolved.incremental_cost_now == 0.0
    assert resolved.eligible is True


def test_provider_model_offerings_are_classified_independently():
    paid = _candidate(
        provider_id="provider-a",
        model_id="x-ai/grok-3",
        nominal_pricing=NominalPricing(input=1.0, output=2.0),
    )
    free = _candidate(
        provider_id="provider-b",
        model_id="x-ai/grok-3",
        free_entitlement=FreeEntitlement(type="renewing-quota", active=True),
    )
    assert resolve_candidate(paid).eligible is False
    assert resolve_candidate(paid).exclusion_reason == "incremental-cost-proven"
    assert resolve_candidate(free).eligible is True


def test_quota_exhaustion_keeps_free_classification():
    candidate = _candidate(
        free_entitlement=FreeEntitlement(type="renewing-quota", active=True),
        runtime=RuntimeState(auth_status="ok", availability="quota-exhausted"),
    )
    resolved = resolve_candidate(candidate)
    assert resolved.free_classification == "free"
    assert resolved.eligible is False
    assert resolved.exclusion_reason == "quota-exhausted"


def test_429_changes_availability_not_free_status():
    candidate = _candidate(free_entitlement=FreeEntitlement(type="perpetual", active=True))
    changed = apply_http_status(candidate, 429)
    assert classify_free(changed) == "free"
    assert changed.runtime.availability == "rate-limited"


def test_401_changes_auth_not_paid_status():
    candidate = _candidate(free_entitlement=FreeEntitlement(type="perpetual", active=True))
    changed = apply_http_status(candidate, 401)
    assert classify_free(changed) == "free"
    assert changed.runtime.auth_status == "failed"
    assert resolve_candidate(changed).exclusion_reason == "auth-failed"


def test_discovery_failure_retains_last_known_good():
    prior = [_candidate(model_id="one"), _candidate(model_id="two")]
    merged = retain_last_known_good(prior, [], refresh_succeeded=False)
    assert [item.model_id for item in merged] == ["one", "two"]
    assert all(item.discovery_stale for item in merged)


def test_unknown_cost_is_not_safe_free():
    candidate = _candidate(nominal_pricing=NominalPricing())
    resolved = resolve_candidate(candidate)
    assert resolved.free_classification == "unknown"
    assert resolved.eligible is False
    assert resolved.exclusion_reason == "incremental-cost-unknown"


def test_zero_input_output_is_not_enough_when_other_charges_possible():
    candidate = _candidate(
        nominal_pricing=NominalPricing(input=0, output=0, other_possible_charges=True)
    )
    resolved = resolve_candidate(candidate)
    assert resolved.free_classification == "unknown"
    assert resolved.eligible is False


def test_sqlite_store_survives_reload(tmp_path):
    path = tmp_path / "free-mesh.sqlite3"
    original = _candidate(
        provider_id="nous_research",
        model_id="Hermes-4-70B",
        capabilities=CandidateCapabilities(text=True, tools=True, context_window=131072),
        evidence=(EvidenceRecord("operator-policy", "always free", 0.99),),
    )
    store = FreeCandidateStore(path)
    store.replace_candidates([original])

    reloaded = FreeCandidateStore(path).load_candidates()
    assert len(reloaded) == 1
    assert reloaded[0].provider_id == "nous-research"
    assert reloaded[0].model_id == "Hermes-4-70B"
    assert reloaded[0].capabilities.tools is True
    assert reloaded[0].evidence[0].source == "operator-policy"
