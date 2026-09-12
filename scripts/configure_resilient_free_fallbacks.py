#!/usr/bin/env python3
"""Install a conservative cross-provider free fallback ring for Hermes.

The primary route (typically OmniRoute auto/best-free) is left untouched.  This
script only adds fallback entries that Hermes can resolve with the credentials
already present on the host.  It never copies API keys into config.yaml.
"""
from __future__ import annotations

from contextlib import suppress
from typing import Any

from agent.auxiliary_client import aux_probe_mode, resolve_provider_client
from agent.backend_identity import BackendIdentity, same_deployment
from hermes_cli.config import load_config, save_config
from hermes_cli.fallback_config import get_fallback_chain
from hermes_cli.models import provider_model_ids


# Independent pools first. OpenCode is deliberately last because OmniRoute's
# free pool already uses the same public Zen egress and a provider-wide 429 there
# is unlikely to improve by immediately retrying it directly.
CANDIDATES: list[tuple[str, tuple[str, ...]]] = [
    ("openrouter", ("openrouter/free",)),
    ("kilocode", ("openrouter/free",)),
    ("groq", ("qwen/qwen3.6-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b")),
    ("opencode-free", (
        "nemotron-3.5-lightning-free",
        "nemotron-3-ultra-free",
        "deepseek-v4-flash-free",
        "mimo-v2.5-free",
    )),
]


def _identity(entry: dict[str, Any]) -> BackendIdentity:
    return BackendIdentity.build(
        provider=entry.get("provider"),
        model=entry.get("model"),
        base_url=entry.get("base_url"),
    )


def _primary_entry(config: dict[str, Any]) -> dict[str, Any] | None:
    model_cfg = config.get("model")
    if not isinstance(model_cfg, dict):
        return None
    provider = str(model_cfg.get("provider") or "").strip()
    model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
    if not provider or not model:
        return None
    entry: dict[str, Any] = {"provider": provider, "model": model}
    base_url = str(model_cfg.get("base_url") or "").strip()
    if base_url:
        entry["base_url"] = base_url.rstrip("/")
    return entry


def _live_models(provider: str) -> list[str]:
    with suppress(Exception):
        return [str(m).strip() for m in provider_model_ids(provider, force_refresh=True) if str(m).strip()]
    return []


def _choose_model(provider: str, preferred: tuple[str, ...]) -> str | None:
    live = _live_models(provider)
    if live:
        lower_to_live = {m.lower(): m for m in live}
        for wanted in preferred:
            if wanted.lower() in lower_to_live:
                return lower_to_live[wanted.lower()]
        # Never guess a paid OpenRouter/Kilo model. Their fallback is only the
        # explicit zero-cost router; absence means this lane is not certifiable.
        if provider in {"openrouter", "kilocode"}:
            return None
        # For Groq/OpenCode, prefer names that are explicitly free/current from
        # our preferred list; otherwise do not pin an arbitrary live model.
        return None
    # A live catalog may be unavailable while the provider is otherwise usable.
    # The preferred ids are curated fallbacks; resolver probing below is still
    # required before anything is persisted.
    return preferred[0] if preferred else None


def _resolvable(provider: str, model: str) -> bool:
    try:
        with aux_probe_mode():
            client, resolved_model = resolve_provider_client(provider, model=model)
        return client is not None and bool(resolved_model or model)
    except Exception:
        return False


def main() -> int:
    config = load_config() or {}
    existing = get_fallback_chain(config)
    primary = _primary_entry(config)
    primary_ident = _identity(primary) if primary else None

    discovered: list[dict[str, Any]] = []
    for provider, preferred in CANDIDATES:
        model = _choose_model(provider, preferred)
        if not model or not _resolvable(provider, model):
            print(f"FREE_FALLBACK_SKIP provider={provider} reason=unconfigured-or-unresolvable")
            continue
        entry = {"provider": provider, "model": model}
        if primary_ident is not None and same_deployment(primary_ident, _identity(entry)):
            continue
        if any(same_deployment(_identity(old), _identity(entry)) for old in existing + discovered):
            continue
        discovered.append(entry)
        print(f"FREE_FALLBACK_READY provider={provider} model={model}")

    # Keep existing operator choices first; add newly discovered independent
    # lanes after them. The runtime skips unavailable entries and advances on
    # rate-limit/5xx/transport failures.
    if discovered:
        config["fallback_providers"] = existing + discovered
        config.pop("fallback_model", None)
        save_config(config)

    print(f"FREE_FALLBACK_CONFIG_OK existing={len(existing)} added={len(discovered)} total={len(existing) + len(discovered)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
