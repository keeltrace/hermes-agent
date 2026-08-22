"""Registry of computer-use providers, and the rule for picking one.

Built-in providers are global. Plugin providers are scoped to the active
Hermes profile, matching the browser/web provider registries. Resolution is
explicit and fail-closed: an unknown configured provider never falls back to
driving the gateway host's desktop.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from agent.computer_use_provider import ComputerUseProvider
from hermes_constants import hermes_home_key

logger = logging.getLogger(__name__)

HOST_PROVIDER_NAME = "local"
_HOST_ALIASES = {"", "local", "cua", "cua-driver", "builtin", "host"}

_providers: Dict[str, ComputerUseProvider] = {}
_scoped_providers: Dict[str, Dict[str, ComputerUseProvider]] = {}
_lock = threading.Lock()


class UnknownComputerUseProvider(LookupError):
    """``computer_use.provider`` names a provider that is not registered."""

    def __init__(self, configured: str, available: List[str]):
        self.configured = configured
        self.available = available
        known = ", ".join(available) or "none"
        super().__init__(
            f"computer_use.provider is set to {configured!r}, but no such provider "
            f"is registered (available: {known}). Install the plugin that provides "
            f"it, or set computer_use.provider to '{HOST_PROVIDER_NAME}' in config.yaml."
        )


def register_provider(
    provider: ComputerUseProvider, *, scope: Optional[str] = None
) -> None:
    """Register a provider globally or for one Hermes profile."""
    if not isinstance(provider, ComputerUseProvider):
        raise TypeError(
            "register_provider() expects a ComputerUseProvider instance, "
            f"got {type(provider).__name__}"
        )

    raw_name = provider.name
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("Computer use provider .name must be a non-empty string")
    name = raw_name.strip()

    with _lock:
        target = _providers if scope is None else _scoped_providers.setdefault(scope, {})
        existing = target.get(name)
        target[name] = provider

    if existing is not None:
        logger.debug("Computer use provider %r replaced %s", name, type(existing).__name__)


def list_providers(*, scope: Optional[str] = None) -> List[ComputerUseProvider]:
    """Every provider visible to one profile, sorted by name."""
    active_scope = scope or hermes_home_key()
    with _lock:
        merged = dict(_providers)
        merged.update(_scoped_providers.get(active_scope, {}))
        providers = list(merged.values())
    return sorted(providers, key=lambda p: p.name)


def get_provider(
    name: str, *, scope: Optional[str] = None
) -> Optional[ComputerUseProvider]:
    """The provider visible under *name* for one profile, or None."""
    if not isinstance(name, str):
        return None

    key = name.strip()
    active_scope = scope or hermes_home_key()
    with _lock:
        return _scoped_providers.get(active_scope, {}).get(key) or _providers.get(key)


def snapshot_registration(
    name: str, *, scope: Optional[str] = None
) -> Optional[ComputerUseProvider]:
    """Exact registration in one slot, for plugin unload restore."""
    key = name.strip()
    with _lock:
        target = _providers if scope is None else _scoped_providers.get(scope, {})
        return target.get(key)


def restore_registration(
    name: str,
    current: ComputerUseProvider,
    previous: Optional[ComputerUseProvider],
    *,
    scope: Optional[str] = None,
) -> bool:
    """Undo one registration only when *current* still owns that exact slot."""
    key = name.strip()
    with _lock:
        target = _providers if scope is None else _scoped_providers.setdefault(scope, {})
        if target.get(key) is not current:
            return False

        if previous is None:
            target.pop(key, None)
        else:
            target[key] = previous

        if scope is not None and not target:
            _scoped_providers.pop(scope, None)

    return True


def resolve_provider(
    configured: Optional[str], *, scope: Optional[str] = None
) -> ComputerUseProvider:
    """Return the provider visible to the active profile.

    Unknown names raise instead of falling back to the host provider.
    """
    name = (configured or "").strip().lower()
    if name in _HOST_ALIASES:
        name = HOST_PROVIDER_NAME

    provider = get_provider(name, scope=scope)
    if provider is None:
        raise UnknownComputerUseProvider(
            name, [p.name for p in list_providers(scope=scope)]
        )
    return provider


def _reset_for_tests() -> None:
    """Clear the registry. **Test-only.**"""
    with _lock:
        _providers.clear()
        _scoped_providers.clear()
