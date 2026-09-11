"""Session-stable token-lean tool exposure policy.

Ordinary chat pays only for the clarification affordance plus Tool Search. Coding
keeps the small high-frequency local execution waist direct to avoid multiplying
model turns. Dispatcher-owned autonomous workers additionally keep terminal
lifecycle receipts direct. Everything else is progressively disclosed.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Optional

SHORT_DIRECT_TOOLS = frozenset({"clarify"})
WORK_DIRECT_TOOLS = frozenset({
    "terminal", "read_file", "write_file", "patch", "search_files", "clarify",
})
AUTONOMOUS_DIRECT_TOOLS = WORK_DIRECT_TOOLS | frozenset({"kanban_complete", "kanban_block"})


def _session_env(name: str, default: str = "") -> str:
    """Read request-local gateway session state before the process environment."""
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(name, default) or default)
    except Exception:
        return str(os.environ.get(name, default) or default)


def token_lean_session_mode(*, config: Optional[dict[str, Any]] = None,
                            platform: Optional[str] = None, cwd: Optional[str] = None) -> str:
    cfg = config if isinstance(config, dict) else {}
    te = cfg.get("token_economy") if isinstance(cfg, dict) else None
    configured = str((te or {}).get("session_mode", "auto") or "auto").strip().lower() if isinstance(te, dict) else "auto"
    if configured in {"short", "work", "autonomous"}:
        return configured

    source = _session_env("HERMES_SESSION_SOURCE", "").strip().lower()
    if os.environ.get("HERMES_KANBAN_TASK") or source in {"kanban", "subagent", "cron"}:
        return "autonomous"

    resolved_platform = (platform or os.environ.get("HERMES_PLATFORM")
                         or _session_env("HERMES_SESSION_PLATFORM", "") or source or "cli")
    try:
        from agent.coding_context import is_coding_context
        if is_coding_context(platform=resolved_platform, cwd=cwd, config=cfg or None):
            return "work"
    except Exception:
        pass
    return "short"


def token_lean_direct_tools(*, config: Optional[dict[str, Any]] = None,
                            platform: Optional[str] = None, cwd: Optional[str] = None) -> frozenset[str]:
    mode = token_lean_session_mode(config=config, platform=platform, cwd=cwd)
    if mode == "autonomous":
        return AUTONOMOUS_DIRECT_TOOLS
    if mode == "work":
        return WORK_DIRECT_TOOLS
    return SHORT_DIRECT_TOOLS


def token_lean_defer_tools(core_tools: Iterable[str] | None = None, *,
                           config: Optional[dict[str, Any]] = None,
                           platform: Optional[str] = None, cwd: Optional[str] = None) -> list[str]:
    """Return core tools hidden behind Tool Search for the current lean mode.

    Order follows ``_HERMES_CORE_TOOLS`` so schema bytes are deterministic and
    provider prompt caches remain effective.
    """
    if core_tools is None:
        from toolsets import _HERMES_CORE_TOOLS
        core_tools = _HERMES_CORE_TOOLS
    direct = token_lean_direct_tools(config=config, platform=platform, cwd=cwd)
    return [name for name in core_tools if name not in direct]


# Backward-compatible name used by older tests/extensions; represents the coding waist.
TOKEN_LEAN_DIRECT_TOOLS = WORK_DIRECT_TOOLS
