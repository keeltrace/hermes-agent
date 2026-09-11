from __future__ import annotations


def test_memory_master_switch_disables_both_stores():
    from tools.memory_tool import get_builtin_memory_store_flags

    assert get_builtin_memory_store_flags({"memory": {"enabled": False}}) == (False, False)
    assert get_builtin_memory_store_flags({"memory": {"enabled": "false"}}) == (False, False)


def test_dispatcher_worker_skips_auto_title(monkeypatch):
    from agent import title_generator

    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_lean")
    monkeypatch.setattr(
        "agent.delegation_context.is_dispatcher_owned_worker_context", lambda: True
    )
    assert title_generator._auto_title_enabled() is False


def test_lean_worker_argv_inlines_task_and_ignores_rules(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    profile = tmp_path / "profile"
    profile.mkdir()
    profile.joinpath("config.yaml").write_text(
        "agent:\n"
        "  inline_kanban_context: true\n"
        "  inline_kanban_context_max_chars: 1000\n"
        "  tool_allowlist: [terminal, kanban_complete, kanban_block]\n"
        "platform_toolsets:\n"
        "  cli: [terminal]\n",
        encoding="utf-8",
    )
    task = kb.Task(
        id="t_lean", title="lean proof", body="run the proof", assignee="lean",
        status="running", priority=0, created_by="test", created_at=1,
        started_at=None, completed_at=None, workspace_kind="dir",
        workspace_path=None, claim_lock="lock", claim_expires=None,
        tenant=None, current_run_id=1,
    )
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    cmd = kbd._worker_argv(task, "lean", str(profile), "/tmp/work")
    assert "--ignore-rules" in cmd
    query = cmd[cmd.index("-q") + 1]
    assert "Execute assigned Kanban task t_lean" in query
    assert "run the proof" in query
    assert "/tmp/work" in query


def test_token_economy_skips_llm_title_upgrade_but_keeps_local_title(monkeypatch):
    from types import SimpleNamespace
    from agent import title_generator
    import agent.token_economy as te

    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(te, "load_settings", lambda: SimpleNamespace(enabled=True, llm_title_upgrade=False))
    assert title_generator._auto_title_enabled() is False
    assert title_generator.derive_title("Fix excessive Hermes token usage") == "Fix excessive Hermes token usage"


def test_token_economy_can_explicitly_reenable_llm_title_upgrade(monkeypatch):
    from types import SimpleNamespace
    from agent import title_generator
    import agent.token_economy as te

    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(te, "load_settings", lambda: SimpleNamespace(enabled=True, llm_title_upgrade=True))
    monkeypatch.setattr(title_generator, "_title_config", lambda: {"enabled": True})
    assert title_generator._auto_title_enabled() is True


def test_token_economy_memory_projection_defaults_off_without_disabling_store(monkeypatch):
    from types import SimpleNamespace
    from agent import agent_init
    import agent.token_economy as te

    class DummyStore:
        pass

    monkeypatch.setattr(
        "tools.memory_tool.get_builtin_memory_config",
        lambda _cfg: {"inject_context": True, "memory_enabled": True, "user_profile_enabled": True},
    )
    monkeypatch.setattr("tools.memory_tool.get_builtin_memory_store_flags", lambda _cfg: (True, True))
    monkeypatch.setattr("tools.memory_tool.MemoryStore", lambda **_kw: DummyStore())
    monkeypatch.setattr(te, "load_settings", lambda: SimpleNamespace(enabled=True, memory_prompt_injection=False))
    agent = SimpleNamespace(enabled_toolsets=["memory"], disabled_toolsets=[])
    agent_init._init_memory(agent, {}, False, "cli")
    assert agent._memory_store is not None
    assert agent._memory_enabled is True
    assert agent._user_profile_enabled is True
    assert agent._memory_prompt_enabled is False
