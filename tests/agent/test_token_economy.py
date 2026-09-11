from __future__ import annotations

from types import SimpleNamespace

from agent.token_economy import (
    TokenEconomySettings,
    _tool_result_retention_cutoff,
    externalize_historical_tool_results,
)


def test_lean_defaults_are_bounded():
    settings = TokenEconomySettings.from_raw({})
    assert settings.compact_prompt is True
    assert settings.tool_result_externalize_chars == 256
    assert settings.tool_result_receipt_chars == 160
    assert settings.live_tool_result_chars == 4000
    assert settings.live_tool_turn_chars == 8000
    assert settings.task_state_projection_chars == 900
    assert settings.llm_title_upgrade is False
    assert settings.background_review_enabled is False
    assert settings.memory_prompt_injection is False
    assert settings.context_file_max_chars == 4000
    assert settings.short_context_ceiling == 32000
    assert settings.work_context_ceiling == 96000
    assert settings.autonomous_context_ceiling == 192000


def test_retention_cutoff_keeps_only_latest_tool_batch():
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "first"},
        {"role": "assistant", "tool_calls": [{"id": "b"}]},
        {"role": "tool", "tool_call_id": "b", "content": "second"},
    ]
    assert _tool_result_retention_cutoff(messages, 1) == 3
    assert _tool_result_retention_cutoff(messages, 2) == 1
    assert _tool_result_retention_cutoff(messages, 0) == len(messages)


def test_externalizer_evicts_older_batch_during_same_user_turn(monkeypatch):
    import agent.token_economy as te
    import agent.token_economy_store as store

    monkeypatch.setattr(te, "load_settings", lambda: TokenEconomySettings(
        enabled=True,
        tool_result_externalize_chars=256,
        tool_result_receipt_chars=320,
        retain_tool_result_turns=1,
    ))

    rows = {
        "a": {"result_id": "tr_old", "tool_name": "terminal", "sha256": "a" * 64,
              "char_count": 4000, "estimated_tokens": 1000, "receipt": "old output archived"},
        "b": {"result_id": "tr_new", "tool_name": "terminal", "sha256": "b" * 64,
              "char_count": 4000, "estimated_tokens": 1000, "receipt": "new output archived"},
    }
    monkeypatch.setattr(store, "get_tool_result_for_call", lambda _sid, call_id: rows.get(call_id))

    payload = {"messages": [
        {"role": "user", "content": "build it"},
        {"role": "assistant", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "A" * 4000},
        {"role": "assistant", "tool_calls": [{"id": "b", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "b", "content": "B" * 4000},
    ]}
    agent = SimpleNamespace(session_id="s1")
    out, saved = externalize_historical_tool_results(payload, agent)
    assert saved > 500
    assert "tr_old" in out["messages"][2]["content"]
    assert out["messages"][4]["content"] == "B" * 4000
    assert payload["messages"][2]["content"] == "A" * 4000  # canonical input untouched


def test_agent_tool_result_budget_is_clamped_only_when_token_economy_enabled(monkeypatch):
    from types import SimpleNamespace
    import agent.token_economy as te
    import agent.tool_executor as executor

    agent = SimpleNamespace(context_compressor=SimpleNamespace(context_length=1_000_000))
    monkeypatch.setattr(te, "load_settings", lambda: SimpleNamespace(
        enabled=True, live_tool_result_chars=4000, live_tool_turn_chars=8000,
    ))
    lean = executor._budget_for_agent(agent)
    assert lean.default_result_size == 4000
    assert lean.turn_budget == 8000
    assert lean.preview_size <= 1200

    monkeypatch.setattr(te, "load_settings", lambda: SimpleNamespace(enabled=False))
    stock = executor._budget_for_agent(agent)
    assert stock.default_result_size == 100000
    assert stock.turn_budget == 200000


def test_automatic_background_review_is_opt_in_under_token_economy(monkeypatch):
    import agent.token_economy as te
    monkeypatch.setattr(te, "load_settings", lambda: TokenEconomySettings(enabled=True, background_review_enabled=False))
    assert te.automatic_background_review_allowed() is False
    monkeypatch.setattr(te, "load_settings", lambda: TokenEconomySettings(enabled=True, background_review_enabled=True))
    assert te.automatic_background_review_allowed() is True
    monkeypatch.setattr(te, "load_settings", lambda: TokenEconomySettings(enabled=False, background_review_enabled=False))
    assert te.automatic_background_review_allowed() is True



def test_executor_archives_exact_output_before_spill(monkeypatch):
    """A result receipt must always point at bytes archived before transcript truncation."""
    import agent.token_economy as te
    import agent.tool_executor as executor

    order = []
    raw = "RAW-" + "x" * 20_000

    def archive(_agent, **kwargs):
        order.append(("archive", kwargs["content"]))
        assert kwargs["content"] == raw
        return {"result_id": "tr_exact"}

    def spill(**kwargs):
        assert order and order[0][0] == "archive"
        order.append(("spill", kwargs["content"]))
        assert kwargs["content"] == raw
        return "[Tool output persisted: /tmp/spill]\npreview only"

    def make_message(name, content, call_id, **_kwargs):
        order.append(("message", content))
        return {"role": "tool", "name": name, "tool_call_id": call_id, "content": content}

    agent = SimpleNamespace(
        _current_tool="terminal",
        verbose_logging=False,
        _touch_activity=lambda *_args, **_kwargs: None,
        _subdirectory_hints=SimpleNamespace(check_tool_call=lambda *_args, **_kwargs: ""),
        _tool_result_content_for_active_model=lambda _name, result: result,
        tool_progress_callback=None,
    )
    ref = executor._ToolCallRef(
        name="terminal", args={"command": "big-output"}, task_id="task", call_id="call-1", trace=[]
    )
    messages = []

    monkeypatch.setattr(te, "archive_tool_result_for_agent", archive)
    monkeypatch.setattr(executor, "maybe_persist_tool_result", spill)
    monkeypatch.setattr(executor, "get_active_env", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(executor, "_record_persisted_path_for_stub", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(executor, "make_tool_result_message", make_message)
    monkeypatch.setattr(executor, "_flush_session_db_after_tool_progress", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(executor, "_safe_callback", lambda *_args, **_kwargs: None)

    result = executor._commit_tool_result(
        agent,
        messages,
        ref,
        raw,
        budget=executor.DEFAULT_BUDGET,
        tool_duration=0.1,
        is_error=False,
        blocked=False,
        effect_disposition=None,
    )

    assert [entry[0] for entry in order] == ["archive", "spill", "message"]
    assert order[0][1] == raw
    assert messages[0]["content"].startswith("[Tool output persisted:")
    assert result is not None


def test_task_state_projection_is_compaction_scoped_and_cache_stable(monkeypatch):
    import agent.token_economy as te
    import agent.token_economy_store as store

    monkeypatch.setattr(te, "load_settings", lambda: TokenEconomySettings(
        enabled=True, task_state_enabled=True, task_state_projection_chars=900, session_mode="work"
    ))
    current = {"state": {"goal": "ship lean Hermes", "phase": "executing", "evidence": ["old"]}}
    monkeypatch.setattr(store, "get_task_state", lambda _sid: current)
    agent = SimpleNamespace(session_id="s1")

    # Full live history already contains the task; adding a sidecar projection is redundant.
    live = {"messages": [{"role": "user", "content": "ship lean Hermes"}]}
    assert te.project_task_state(live, agent) is live

    # A compression summary creates the first projection epoch.
    compressed = {"messages": [
        {"role": "system", "content": "Conversation summary: prior implementation work compacted", "_compressed_summary": True},
        {"role": "user", "content": "continue"},
    ]}
    first = te.project_task_state(compressed, agent)
    assert first is not compressed
    assert "<task_state>" in first["messages"][-1]["content"]
    assert '"old"' in first["messages"][-1]["content"]

    # Live task state may advance after more tools, but the projection stays byte-stable
    # for this summary epoch so the provider can reuse the prefix cache.
    current["state"]["evidence"] = ["new"]
    second = te.project_task_state(compressed, agent)
    assert second == first
    assert '"new"' not in second["messages"][-1]["content"]

    # A new summary epoch refreshes from the durable state.
    newer = {"messages": [
        {"role": "system", "content": "Conversation summary: second compaction", "_compressed_summary": True},
        {"role": "user", "content": "continue again"},
    ]}
    third = te.project_task_state(newer, agent)
    assert '"new"' in third["messages"][-1]["content"]
    assert agent._token_economy_task_projection_epoch != te._task_projection_epoch(compressed["messages"])


def test_session_reset_clears_token_economy_projection_state():
    from run_agent import AIAgent

    class Dummy:
        session_id = "new-session"
        context_compressor = None
        _session_db = None
        _token_economy_pending_request_id = "old-request"
        _token_economy_compaction_saved_tokens = 123
        _token_economy_compaction_reason = "old-compaction"
        _token_economy_task_projection_epoch = "old-epoch"
        _token_economy_task_projection_text = "old-state"
        _frozen_workspace_snapshot = ("/tmp", "old")
        def _transition_context_engine_session(self, **_kwargs):
            pass

    obj = Dummy()
    AIAgent.reset_session_state(obj)
    assert obj._token_economy_pending_request_id is None
    assert obj._token_economy_compaction_saved_tokens == 0
    assert obj._token_economy_compaction_reason == ""
    assert obj._token_economy_task_projection_epoch == ""
    assert obj._token_economy_task_projection_text == ""


def test_session_mode_honors_request_local_gateway_context(monkeypatch):
    import agent.coding_context as coding_context
    import agent.token_economy as te
    import gateway.session_context as session_context

    settings = TokenEconomySettings(enabled=True, session_mode="auto")
    agent = SimpleNamespace(platform="cli", _kanban_task_id=None)

    monkeypatch.setattr(
        session_context, "get_session_env",
        lambda name, default="": "cron" if name == "HERMES_SESSION_SOURCE" else default,
    )
    assert te.session_mode(agent, settings) == "autonomous"

    seen = {}
    monkeypatch.setattr(
        session_context, "get_session_env",
        lambda name, default="": (
            "" if name == "HERMES_SESSION_SOURCE"
            else "desktop" if name == "HERMES_SESSION_PLATFORM"
            else default
        ),
    )
    def fake_coding(*, platform=None, **_kwargs):
        seen["platform"] = platform
        return True
    monkeypatch.setattr(coding_context, "is_coding_context", fake_coding)
    assert te.session_mode(agent, settings) == "work"
    assert seen["platform"] == "desktop"

    explicit = TokenEconomySettings(enabled=True, session_mode="short")
    assert te.session_mode(agent, explicit) == "short"


def test_configured_primary_provider_is_secret_free_and_shape_tolerant():
    import agent.token_economy as te

    assert te.configured_primary_provider({}) == "auto"
    assert te.configured_primary_provider({"model": "model-id"}) == "auto"
    assert te.configured_primary_provider({"model": {"provider": " MoA ", "api_key": "secret"}}) == "moa"
    assert te.configured_primary_provider({"model": {"default": {"provider": "OpenRouter", "model": "x"}}}) == "openrouter"
    assert te.configured_primary_provider({"model": {"provider": "omni-local", "default": {"provider": "moa"}}}) == "omni-local"


def test_responses_function_call_outputs_externalize_and_account_as_tools(monkeypatch):
    import agent.token_economy as te
    import agent.token_economy_store as store

    settings = TokenEconomySettings(
        enabled=True, tool_result_externalize_chars=256, tool_result_receipt_chars=320,
        retain_tool_result_turns=1,
    )
    rows = {
        "call_old": {"result_id": "tr_old", "tool_name": "terminal", "sha256": "a" * 64,
                     "char_count": 4000, "estimated_tokens": 1000, "receipt": "old output archived"},
        "call_new": {"result_id": "tr_new", "tool_name": "terminal", "sha256": "b" * 64,
                     "char_count": 4000, "estimated_tokens": 1000, "receipt": "new output archived"},
    }
    payload = {"input": [
        {"role": "user", "content": "build it"},
        {"type": "function_call", "call_id": "call_old", "name": "terminal", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_old", "output": "A" * 4000},
        {"type": "function_call", "call_id": "call_new", "name": "terminal", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_new", "output": "B" * 4000},
    ]}
    monkeypatch.setattr(te, "load_settings", lambda: settings)
    monkeypatch.setattr(store, "get_tool_result_for_call", lambda _sid, cid: rows.get(cid))

    projected, saved = te.externalize_historical_tool_results(payload, SimpleNamespace(session_id="s1"))
    assert saved > 500
    assert "tr_old" in projected["input"][2]["output"]
    assert projected["input"][4]["output"] == "B" * 4000
    assert payload["input"][2]["output"] == "A" * 4000

    totals, _fingerprints = te._message_components(projected["input"])
    assert totals["retained_tool_results"] > 0
    assert te._role(projected["input"][2]) == "tool"
    assert "tr_old" in te._content(projected["input"][2])


def test_responses_multimodal_output_is_left_to_image_retirement(monkeypatch):
    import agent.token_economy as te
    import agent.token_economy_store as store

    settings = TokenEconomySettings(enabled=True, tool_result_externalize_chars=256, retain_tool_result_turns=0)
    payload = {"input": [
        {"type": "function_call", "call_id": "call_img", "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_img", "output": [
            {"type": "input_text", "text": "x" * 1000},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        ]},
    ]}
    monkeypatch.setattr(te, "load_settings", lambda: settings)
    monkeypatch.setattr(store, "get_tool_result_for_call", lambda *_args: {
        "result_id": "tr_img", "tool_name": "read_file", "sha256": "c" * 64,
        "char_count": 1000, "estimated_tokens": 250, "receipt": "image result",
    })
    projected, saved = te.externalize_historical_tool_results(payload, SimpleNamespace(session_id="s1"))
    assert projected is payload
    assert saved == 0


def test_canonical_projection_precedes_responses_id_rewrite(monkeypatch):
    import agent.token_economy as te
    import agent.token_economy_store as store
    from agent.codex_responses_adapter import _chat_messages_to_responses_input
    from agent.turn_api_request import _project_token_economy_messages

    settings = TokenEconomySettings(
        enabled=True, tool_result_externalize_chars=256, tool_result_receipt_chars=320,
        retain_tool_result_turns=1, session_mode="short",
    )
    # Responses clamps this >64-char ID. The archive remains keyed by the original.
    old_id = "call_" + "x" * 100
    new_id = "call_new"
    rows = {
        old_id: {"result_id": "tr_long_id", "tool_name": "terminal", "sha256": "d" * 64,
                 "char_count": 4000, "estimated_tokens": 1000, "receipt": "long-id output archived"},
    }
    canonical = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": old_id, "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": old_id, "name": "terminal", "content": "A" * 4000},
        {"role": "assistant", "content": "", "tool_calls": [{"id": new_id, "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": new_id, "name": "terminal", "content": "B" * 4000},
    ]
    agent = SimpleNamespace(session_id="s1")
    monkeypatch.setattr(te, "load_settings", lambda: settings)
    monkeypatch.setattr(store, "get_tool_result_for_call", lambda _sid, cid: rows.get(cid))

    projected, saved = _project_token_economy_messages(agent, canonical)
    assert saved > 500
    assert "tr_long_id" in projected[2]["content"]
    assert canonical[2]["content"] == "A" * 4000

    wire = _chat_messages_to_responses_input(projected)
    outputs = [item for item in wire if item.get("type") == "function_call_output"]
    assert "tr_long_id" in outputs[0]["output"]
    assert outputs[0]["call_id"] != old_id  # proves projection happened before clamping


def test_multimodal_chat_tool_result_is_not_externalized(monkeypatch):
    import agent.token_economy as te
    import agent.token_economy_store as store

    settings = TokenEconomySettings(enabled=True, tool_result_externalize_chars=256, retain_tool_result_turns=0)
    payload = {"messages": [
        {"role": "assistant", "tool_calls": [{"id": "img", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "img", "content": [
            {"type": "text", "text": "x" * 1000},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
    ]}
    monkeypatch.setattr(te, "load_settings", lambda: settings)
    monkeypatch.setattr(store, "get_tool_result_for_call", lambda *_args: {
        "result_id": "tr_img", "tool_name": "read_file", "sha256": "e" * 64,
        "char_count": 1000, "estimated_tokens": 250, "receipt": "image result",
    })
    projected, saved = te.externalize_historical_tool_results(payload, SimpleNamespace(session_id="s1"))
    assert projected is payload
    assert saved == 0


def test_aggressive_compressor_policy_scales_prune_before_context_cap(monkeypatch):
    import agent.token_economy as te
    from agent.context_compressor import ContextCompressor

    cases = [
        ("short", 8000, 6000, 2000),
        ("work", 16000, 10666, 4000),
        ("autonomous", 24000, 16000, 4096),
    ]
    for mode, cap, prune_trigger, min_reclaim in cases:
        settings = TokenEconomySettings(
            enabled=True, session_mode=mode,
            short_context_ceiling=8000, work_context_ceiling=16000, autonomous_context_ceiling=24000,
        )
        compressor = ContextCompressor(
            model="test/model", config_context_length=128000,
            proactive_prune_tokens=0, proactive_prune_min_result_chars=8000,
            proactive_prune_min_reclaim_tokens=4096,
        )
        agent = SimpleNamespace(context_compressor=compressor, platform="cli")
        monkeypatch.setattr(te, "load_settings", lambda settings=settings: settings)
        assert te.apply_compressor_policy(agent)
        assert compressor.threshold_tokens_cap == cap
        assert compressor.proactive_prune_tokens == prune_trigger
        assert compressor.proactive_prune_min_result_chars == 2000
        assert compressor.proactive_prune_min_reclaim_tokens == min_reclaim


def test_projection_happens_before_cache_content_wrapping(monkeypatch):
    import agent.token_economy as te
    import agent.token_economy_store as store
    from agent.prompt_caching import build_prompt_cache_plan
    from agent.turn_api_request import _project_token_economy_messages

    settings = TokenEconomySettings(
        enabled=True, tool_result_externalize_chars=256, tool_result_receipt_chars=160,
        retain_tool_result_turns=1, session_mode="short",
    )
    monkeypatch.setattr(te, "load_settings", lambda: settings)
    monkeypatch.setattr(store, "get_tool_result_for_call", lambda _sid, cid: (
        {"result_id": "tr_cache", "tool_name": "terminal", "sha256": "f" * 64,
         "char_count": 4000, "estimated_tokens": 1000, "receipt": "ok"}
        if cid == "old" else None
    ))
    canonical = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "tool_calls": [{"id": "old", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "old", "content": "A" * 4000},
        {"role": "assistant", "tool_calls": [{"id": "new", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "new", "content": "B" * 4000},
    ]
    projected, saved = _project_token_economy_messages(SimpleNamespace(session_id="s1"), canonical)
    assert saved > 500
    assert isinstance(projected[2]["content"], str)
    assert "tr_cache" in projected[2]["content"]
    assert canonical[2]["content"] == "A" * 4000

    plan = build_prompt_cache_plan(
        projected, [], cache_ttl="5m", native_anthropic=False, tool_part_markers=True
    )
    wrapped = plan.messages[2]["content"]
    assert isinstance(wrapped, list)
    assert "tr_cache" in wrapped[0]["text"]
    assert len(wrapped[0]["text"]) < 700


def test_token_economy_compression_tail_and_summary_fit_inside_each_mode(monkeypatch):
    import agent.token_economy as te
    from agent.context_compressor import ContextCompressor

    cases = [
        ("short", 8000, 2500, 512, 768),
        ("work", 16000, 5000, 768, 1536),
        ("autonomous", 24000, 7000, 1024, 2048),
    ]
    sample = [{"role": "user", "content": "x" * 20_000}]
    for mode, cap, tail, summary_min, summary_max in cases:
        settings = TokenEconomySettings(
            enabled=True, session_mode=mode,
            short_context_ceiling=8000, work_context_ceiling=16000, autonomous_context_ceiling=24000,
        )
        compressor = ContextCompressor(model="test/model", config_context_length=128000)
        agent = SimpleNamespace(context_compressor=compressor, platform="cli")
        monkeypatch.setattr(te, "load_settings", lambda settings=settings: settings)
        assert te.apply_compressor_policy(agent)
        assert compressor.threshold_tokens_cap == cap
        assert compressor.tail_token_budget == tail
        assert compressor._runtime_min_summary_tokens_override == summary_min
        assert compressor.max_summary_tokens == summary_max
        budget = compressor._compute_summary_budget(sample)
        assert summary_min <= budget <= summary_max
        assert tail + summary_max < cap

        # Model/provider rotation must preserve the runtime caps.
        compressor.update_model("other/model", 256000, provider="custom")
        assert compressor.tail_token_budget == tail
        assert compressor.max_summary_tokens == summary_max
        assert compressor._compute_summary_budget(sample) <= summary_max


def test_stock_compressor_keeps_stock_summary_and_tail_floors():
    from agent.context_compressor import ContextCompressor, LEAN_TAIL_FLOOR_TOKENS, _MIN_SUMMARY_TOKENS

    compressor = ContextCompressor(model="test/model", config_context_length=128000)
    sample = [{"role": "user", "content": "x" * 20_000}]
    assert compressor.tail_token_budget >= LEAN_TAIL_FLOOR_TOKENS
    assert compressor._compute_summary_budget(sample) >= _MIN_SUMMARY_TOKENS


def test_shipped_token_economy_master_default_is_off_until_guarded_activation():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["token_economy"]["enabled"] is False
    assert TokenEconomySettings().enabled is False
