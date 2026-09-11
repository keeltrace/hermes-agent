from __future__ import annotations

from types import SimpleNamespace


class _MemoryManager:
    def __init__(self):
        self.turn_starts = 0
        self.prefetches = 0
        self.syncs = 0
        self.queued = 0

    def on_turn_start(self, *_args, **_kwargs):
        self.turn_starts += 1

    def prefetch_all(self, *_args, **_kwargs):
        self.prefetches += 1
        return "remembered context"

    def build_system_prompt(self):
        raise AssertionError("external memory system prompt must not render when projection is disabled")

    def sync_all(self, *_args, **_kwargs):
        self.syncs += 1

    def queue_prefetch_all(self, *_args, **_kwargs):
        self.queued += 1


def _settings(*, enabled=True, inject=False):
    return SimpleNamespace(enabled=enabled, memory_prompt_injection=inject)


def test_external_memory_system_block_is_suppressed_with_prompt_projection_disabled():
    from agent.system_prompt import _memory_parts

    manager = _MemoryManager()
    agent = SimpleNamespace(
        _memory_store=None,
        _memory_manager=manager,
        _memory_prompt_enabled=False,
    )
    assert _memory_parts(agent) == []


def test_turn_start_keeps_provider_lifecycle_but_skips_automatic_prefetch(monkeypatch):
    from agent.turn_context import _memory_turn_start_and_prefetch

    manager = _MemoryManager()
    agent = SimpleNamespace(
        _memory_manager=manager,
        _user_turn_count=1,
        session_id="s1",
        _emit_status=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("agent.token_economy.load_settings", lambda: _settings(enabled=True, inject=False))
    assert _memory_turn_start_and_prefetch(agent, "substantive user request") == ""
    assert manager.turn_starts == 1
    assert manager.prefetches == 0


def test_turn_start_prefetch_is_restored_when_projection_is_enabled(monkeypatch):
    from agent.turn_context import _memory_turn_start_and_prefetch

    manager = _MemoryManager()
    agent = SimpleNamespace(
        _memory_manager=manager,
        _user_turn_count=1,
        session_id="s1",
        _emit_status=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("agent.token_economy.load_settings", lambda: _settings(enabled=True, inject=True))
    assert _memory_turn_start_and_prefetch(agent, "substantive user request") == "remembered context"
    assert manager.prefetches == 1


def test_end_of_turn_sync_persists_memory_but_does_not_queue_unused_recall(monkeypatch):
    from run_agent import AIAgent

    manager = _MemoryManager()
    agent = SimpleNamespace(
        _memory_manager=manager,
        session_id="s1",
        _turn_author=None,
    )
    monkeypatch.setattr("agent.token_economy.load_settings", lambda: _settings(enabled=True, inject=False))
    AIAgent._sync_external_memory_for_turn(
        agent,
        original_user_message="remember this durable fact",
        final_response="done",
        interrupted=False,
        messages=[],
    )
    assert manager.syncs == 1
    assert manager.queued == 0


def test_end_of_turn_queued_recall_is_restored_when_projection_is_enabled(monkeypatch):
    from run_agent import AIAgent

    manager = _MemoryManager()
    agent = SimpleNamespace(
        _memory_manager=manager,
        session_id="s1",
        _turn_author=None,
    )
    monkeypatch.setattr("agent.token_economy.load_settings", lambda: _settings(enabled=True, inject=True))
    AIAgent._sync_external_memory_for_turn(
        agent,
        original_user_message="remember this durable fact",
        final_response="done",
        interrupted=False,
        messages=[],
    )
    assert manager.syncs == 1
    assert manager.queued == 1
