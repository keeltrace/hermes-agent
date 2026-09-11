from __future__ import annotations

from types import SimpleNamespace

import agent.system_prompt as sp


def _agent(**overrides):
    base = dict(
        valid_tool_names={"terminal", "read_file", "write_file", "patch", "search_files", "clarify"},
        model="qwen/qwen3.6-27b",
        platform="cli",
        provider="custom",
        _task_completion_guidance=True,
        _parallel_tool_call_guidance=True,
        _tool_use_enforcement=True,
        _execution_guidance=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_compact_guidance_collapses_duplicate_execution_prose(monkeypatch):
    settings = SimpleNamespace(enabled=True, compact_prompt=True)
    monkeypatch.setattr("agent.token_economy.load_settings", lambda: settings)
    monkeypatch.setattr("agent.token_economy.session_mode", lambda _agent, _settings: "work")
    text = "\n".join(part for part in sp._guidance_parts(_agent()) if part)
    assert text == sp._LEAN_EXECUTION_GUIDANCE
    assert len(text) < 400


def test_token_economy_master_off_restores_stock_guidance(monkeypatch):
    settings = SimpleNamespace(enabled=False, compact_prompt=True)
    monkeypatch.setattr("agent.token_economy.load_settings", lambda: settings)
    monkeypatch.setattr("agent.token_economy.session_mode", lambda _agent, _settings: "work")
    text = "\n".join(part for part in sp._guidance_parts(_agent()) if part)
    assert sp.TASK_COMPLETION_GUIDANCE in text
    assert sp.PARALLEL_TOOL_CALL_GUIDANCE in text
    assert sp.TOOL_USE_ENFORCEMENT_GUIDANCE in text
    assert len(text) > len(sp._LEAN_EXECUTION_GUIDANCE) * 3


def test_short_compact_mode_uses_no_environment_hint(monkeypatch):
    settings = SimpleNamespace(enabled=True, compact_prompt=True)
    monkeypatch.setattr("agent.token_economy.load_settings", lambda: settings)
    monkeypatch.setattr("agent.token_economy.session_mode", lambda _agent, _settings: "short")
    compact, mode = sp._token_economy_prompt_mode(_agent())
    assert compact is True
    assert mode == "short"
