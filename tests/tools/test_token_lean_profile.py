from __future__ import annotations

from tools.token_lean_profile import (
    AUTONOMOUS_DIRECT_TOOLS,
    SHORT_DIRECT_TOOLS,
    WORK_DIRECT_TOOLS,
    token_lean_defer_tools,
    token_lean_direct_tools,
    token_lean_session_mode,
)


def _cfg(mode: str = "auto") -> dict:
    return {"token_economy": {"enabled": True, "session_mode": mode}, "agent": {"coding_context": "off"}}


def test_short_mode_pays_only_for_clarify_before_discovery_bridge():
    assert SHORT_DIRECT_TOOLS == {"clarify"}
    assert token_lean_direct_tools(config=_cfg("short")) == {"clarify"}


def test_work_mode_keeps_only_high_frequency_local_execution_waist():
    assert WORK_DIRECT_TOOLS == {
        "terminal", "read_file", "write_file", "patch", "search_files", "clarify",
    }
    assert token_lean_direct_tools(config=_cfg("work")) == WORK_DIRECT_TOOLS
    assert "web_search" not in WORK_DIRECT_TOOLS
    assert "process_manage" not in WORK_DIRECT_TOOLS
    assert "browser_navigate" not in WORK_DIRECT_TOOLS
    assert "skills_list" not in WORK_DIRECT_TOOLS


def test_autonomous_mode_keeps_terminal_lifecycle_receipts_direct():
    assert AUTONOMOUS_DIRECT_TOOLS == WORK_DIRECT_TOOLS | {"kanban_complete", "kanban_block"}
    assert token_lean_direct_tools(config=_cfg("autonomous")) == AUTONOMOUS_DIRECT_TOOLS


def test_explicit_modes_override_environment(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    assert token_lean_session_mode(config=_cfg("short")) == "short"
    assert token_lean_session_mode(config=_cfg("work")) == "work"


def test_kanban_environment_auto_resolves_autonomous(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    assert token_lean_session_mode(config=_cfg("auto")) == "autonomous"


def test_defer_list_is_mode_specific_and_preserves_core_order():
    core = [
        "terminal", "browser_navigate", "skills_list", "read_file", "memory", "web_search",
        "kanban_complete", "clarify",
    ]
    short = token_lean_defer_tools(core, config=_cfg("short"))
    work = token_lean_defer_tools(core, config=_cfg("work"))
    autonomous = token_lean_defer_tools(core, config=_cfg("autonomous"))
    assert short == [
        "terminal", "browser_navigate", "skills_list", "read_file", "memory", "web_search", "kanban_complete",
    ]
    assert work == ["browser_navigate", "skills_list", "memory", "web_search", "kanban_complete"]
    assert autonomous == ["browser_navigate", "skills_list", "memory", "web_search"]


def test_defer_list_is_deterministic_including_duplicate_names():
    core = ["memory", "terminal", "memory", "browser_click", "web_search"]
    assert token_lean_defer_tools(core, config=_cfg("work")) == [
        "memory", "memory", "browser_click", "web_search",
    ]


def test_model_tool_cache_scope_tracks_resolved_token_economy_mode(monkeypatch):
    import hermes_cli.config as cfg_mod
    import model_tools

    cfg = _cfg("auto")
    monkeypatch.setattr(cfg_mod, "load_config_readonly", lambda: cfg)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    short_scope = model_tools._token_economy_projection_cache_scope()
    assert short_scope == ("token-economy", "short")

    monkeypatch.setenv("HERMES_SESSION_SOURCE", "cron")
    autonomous_scope = model_tools._token_economy_projection_cache_scope()
    assert autonomous_scope == ("token-economy", "autonomous")
    assert autonomous_scope != short_scope


def test_model_tool_cache_scope_collapses_to_stock_when_master_off(monkeypatch):
    import hermes_cli.config as cfg_mod
    import model_tools

    cfg = _cfg("auto")
    cfg["token_economy"]["enabled"] = False
    monkeypatch.setattr(cfg_mod, "load_config_readonly", lambda: cfg)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "cron")
    assert model_tools._token_economy_projection_cache_scope() == ("stock", "")


def test_request_local_gateway_source_wins_over_stale_process_env(monkeypatch):
    import gateway.session_context as session_context

    cfg = _cfg("auto")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "cli")
    monkeypatch.setattr(
        session_context, "get_session_env",
        lambda name, default="": "cron" if name == "HERMES_SESSION_SOURCE" else default,
    )
    assert token_lean_session_mode(config=cfg) == "autonomous"


def test_request_local_gateway_platform_flows_into_coding_detection(monkeypatch):
    import agent.coding_context as coding_context
    import gateway.session_context as session_context

    cfg = _cfg("auto")
    seen = {}
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)
    monkeypatch.setattr(
        session_context, "get_session_env",
        lambda name, default="": "desktop" if name == "HERMES_SESSION_PLATFORM" else "" if name == "HERMES_SESSION_SOURCE" else default,
    )
    def fake_is_coding_context(*, platform=None, cwd=None, config=None):
        seen["platform"] = platform
        return True
    monkeypatch.setattr(coding_context, "is_coding_context", fake_is_coding_context)
    assert token_lean_session_mode(config=cfg) == "work"
    assert seen["platform"] == "desktop"
