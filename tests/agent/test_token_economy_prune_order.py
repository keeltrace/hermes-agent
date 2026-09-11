from __future__ import annotations

from types import SimpleNamespace


def test_prune_first_is_inert_when_token_economy_is_disabled(monkeypatch):
    import agent.token_economy as te
    from agent.turn_preflight import _token_economy_prune_before_llm

    monkeypatch.setattr(te, "load_settings", lambda: SimpleNamespace(enabled=False))

    class Compressor:
        def prune_tool_results_only(self, *_args, **_kwargs):
            raise AssertionError("stock mode must not invoke the token-economy prune-first hook")

    messages = [{"role": "user", "content": "hello"}]
    result, tokens, count = _token_economy_prune_before_llm(
        SimpleNamespace(), Compressor(), messages, 12_345, reason="test"
    )
    assert result is messages
    assert tokens == 12_345
    assert count == 0


def test_prune_first_uses_committed_deterministic_reclamation_before_llm(monkeypatch):
    import agent.token_economy as te
    from agent.turn_preflight import _token_economy_prune_before_llm

    monkeypatch.setattr(te, "load_settings", lambda: SimpleNamespace(enabled=True))
    monkeypatch.setattr(te, "note_compaction", lambda *_args, **_kwargs: 2_000)

    original = [
        {"role": "user", "content": "do work"},
        {"role": "tool", "content": "x" * 20_000},
    ]
    pruned = [
        original[0],
        {"role": "tool", "content": "[archived tool result: tr_123]"},
    ]

    class Compressor:
        def prune_tool_results_only(self, messages, current_tokens=None):
            assert messages is original
            assert current_tokens == 12_345
            return pruned, 1

    result, tokens, count = _token_economy_prune_before_llm(
        SimpleNamespace(), Compressor(), original, 12_345, reason="test"
    )
    assert result is pruned
    assert tokens == 10_345
    assert count == 1


def test_prune_first_rejects_bogus_in_place_count(monkeypatch):
    import agent.token_economy as te
    from agent.turn_preflight import _token_economy_prune_before_llm

    monkeypatch.setattr(te, "load_settings", lambda: SimpleNamespace(enabled=True))
    messages = [{"role": "tool", "content": "keep me"}]

    class Compressor:
        def prune_tool_results_only(self, incoming, current_tokens=None):
            return incoming, 99

    result, tokens, count = _token_economy_prune_before_llm(
        SimpleNamespace(), Compressor(), messages, 9_000, reason="test"
    )
    assert result is messages
    assert tokens == 9_000
    assert count == 0


def test_prune_first_failure_is_fail_open(monkeypatch):
    import agent.token_economy as te
    from agent.turn_preflight import _token_economy_prune_before_llm

    monkeypatch.setattr(te, "load_settings", lambda: SimpleNamespace(enabled=True))
    messages = [{"role": "tool", "content": "keep me"}]

    class Compressor:
        def prune_tool_results_only(self, *_args, **_kwargs):
            raise RuntimeError("disk temporarily unavailable")

    result, tokens, count = _token_economy_prune_before_llm(
        SimpleNamespace(), Compressor(), messages, 9_000, reason="test"
    )
    assert result is messages
    assert tokens == 9_000
    assert count == 0
