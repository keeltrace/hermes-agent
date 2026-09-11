"""Token-economy policy, request accounting, and compact provider projections.

Canonical state stays durable while provider payloads carry only the minimum
recoverable working set. The Token Ledger stores counts and hashes, never prompt
or message bodies.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)
_SKILLS_RE = re.compile(r"<available_skills>.*?</available_skills>", re.DOTALL)
_TASK_OPEN, _TASK_CLOSE = "<task_state>", "</task_state>"
_RESULT_OPEN, _RESULT_CLOSE = "<tool-result-ref>", "</tool-result-ref>"
_SUMMARY_HINTS = ("historical task snapshot", "context compression summary", "conversation summary")


@dataclass(frozen=True)
class TokenEconomySettings:
    enabled: bool = False
    ledger_enabled: bool = True
    session_mode: str = "auto"
    tool_result_externalize_chars: int = 256
    tool_result_receipt_chars: int = 160
    retain_tool_result_turns: int = 1
    live_tool_result_chars: int = 4000
    live_tool_turn_chars: int = 8000
    task_state_enabled: bool = True
    task_state_projection_chars: int = 900
    compact_prompt: bool = True
    llm_title_upgrade: bool = False
    background_review_enabled: bool = False
    memory_prompt_injection: bool = False
    context_file_max_chars: int = 4000
    short_context_ceiling: int = 32000
    work_context_ceiling: int = 96000
    autonomous_context_ceiling: int = 192000

    @classmethod
    def from_raw(cls, raw: Any) -> "TokenEconomySettings":
        raw = raw if isinstance(raw, dict) else {}
        def flag(key: str, default: bool) -> bool:
            value = raw.get(key, default)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                value = value.strip().lower()
                if value in {"1", "true", "yes", "on"}: return True
                if value in {"0", "false", "no", "off"}: return False
            return default
        def num(key: str, default: int, low: int, high: int) -> int:
            try: value = int(raw.get(key, default))
            except (TypeError, ValueError): value = default
            return max(low, min(value, high))
        mode = str(raw.get("session_mode", "auto") or "auto").strip().lower()
        if mode not in {"auto", "short", "work", "autonomous"}: mode = "auto"
        return cls(
            enabled=flag("enabled", False), ledger_enabled=flag("ledger_enabled", True), session_mode=mode,
            tool_result_externalize_chars=num("tool_result_externalize_chars", 256, 256, 2_000_000),
            tool_result_receipt_chars=num("tool_result_receipt_chars", 160, 160, 4000),
            retain_tool_result_turns=num("retain_tool_result_turns", 1, 0, 20),
            live_tool_result_chars=num("live_tool_result_chars", 4000, 4000, 100000),
            live_tool_turn_chars=num("live_tool_turn_chars", 8000, 8000, 200000),
            task_state_enabled=flag("task_state_enabled", True),
            task_state_projection_chars=num("task_state_projection_chars", 900, 300, 8000),
            compact_prompt=flag("compact_prompt", True),
            llm_title_upgrade=flag("llm_title_upgrade", False),
            background_review_enabled=flag("background_review_enabled", False),
            memory_prompt_injection=flag("memory_prompt_injection", False),
            context_file_max_chars=num("context_file_max_chars", 4000, 2000, 100000),
            short_context_ceiling=num("short_context_ceiling", 32000, 8000, 1_000_000),
            work_context_ceiling=num("work_context_ceiling", 96000, 8000, 1_000_000),
            autonomous_context_ceiling=num("autonomous_context_ceiling", 192000, 8000, 1_000_000),
        )


def load_settings() -> TokenEconomySettings:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return TokenEconomySettings.from_raw(cfg.get("token_economy"))
    except Exception:
        return TokenEconomySettings()


def _canon(value: Any) -> str:
    try: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception: return repr(value)


def _tokens(value: Any) -> int:
    text = value if isinstance(value, str) else _canon(value)
    return (len(text) + 3) // 4 if text else 0


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8", errors="replace")).hexdigest()


def _role(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    if message.get("type") == "function_call_output":
        return "tool"
    return str(message.get("role") or "")


def _content(message: Any) -> str:
    if not isinstance(message, dict):
        return str(message)
    value = message.get("content")
    if value is None and message.get("type") == "function_call_output":
        value = message.get("output")
    return value if isinstance(value, str) else _canon(value) if value is not None else ""


def _wire_messages(payload: Dict[str, Any]) -> List[Any]:
    value = payload.get("messages")
    if isinstance(value, list): return list(value)
    value = payload.get("input")
    return list(value) if isinstance(value, list) else []


def _wire_system(payload: Dict[str, Any], messages: Sequence[Any]) -> str:
    for key in ("system", "instructions"):
        value = payload.get(key)
        if isinstance(value, str): return value
    return "\n\n".join(_content(m) for m in messages if _role(m) == "system")


def _system_components(agent: Any, actual_system: str) -> Dict[str, int]:
    """Semantic split whose sum is forced to the actual wire-system estimate."""
    try:
        from agent.system_prompt import build_system_prompt_parts, _memory_parts
        parts = build_system_prompt_parts(agent)
        stable, context, volatile = (parts.get(k, "") or "" for k in ("stable", "context", "volatile"))
        rendered = "\n\n".join(x for x in (stable, context, volatile) if x)
        match = _SKILLS_RE.search(rendered)
        skills = match.group(0) if match else ""
        memory = "\n\n".join(str(x) for x in _memory_parts(agent) if x)
        rules_t, skills_t, memory_t = _tokens(context), _tokens(skills), _tokens(memory)
        system_t = max(0, _tokens(rendered) - rules_t - skills_t - memory_t)
        wire_total = _tokens(actual_system)
        if wire_total:
            system_t = max(0, system_t + wire_total - (system_t + rules_t + skills_t + memory_t))
        return {"system": system_t, "rules": rules_t, "skills": skills_t, "memory": memory_t}
    except Exception:
        return {"system": _tokens(actual_system), "rules": 0, "skills": 0, "memory": 0}


def _summary_message(message: Any) -> bool:
    if not isinstance(message, dict): return False
    if message.get("_compressed_summary") or message.get("compacted"): return True
    text = _content(message).lower()[:600]
    return any(hint in text for hint in _SUMMARY_HINTS)


def _message_components(messages: Sequence[Any]) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    totals = {"conversation": 0, "retained_tool_results": 0, "compacted_summaries": 0, "task_state": 0}
    fingerprints: List[Dict[str, Any]] = []
    for message in messages:
        if _role(message) == "system": continue
        amount = _tokens(message)
        if _role(message) == "tool": category = "retained_tool_results"
        elif _TASK_OPEN in _content(message): category = "task_state"
        elif _summary_message(message): category = "compacted_summaries"
        else: category = "conversation"
        totals[category] += amount
        fingerprints.append({"hash": _sha(message), "tokens": amount, "category": category})
    return totals, fingerprints


def _last_ledger_row(session_id: str) -> Optional[Dict[str, Any]]:
    try:
        from agent.token_economy_store import recent_token_requests
        rows = recent_token_requests(session_id, 1)
        return rows[0] if rows else None
    except Exception:
        return None


def _row_hashes(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    try: return json.loads((row or {}).get("component_hashes_json") or "{}")
    except Exception: return {}


def _common_prefix(current: List[Dict[str, Any]], previous: List[Dict[str, Any]]) -> int:
    total = 0
    for cur, old in zip(current, previous):
        if cur.get("hash") != old.get("hash"): break
        total += int(cur.get("tokens") or 0)
    return total


def estimate_request_components(agent: Any, payload: Dict[str, Any], *, session_id: str) -> Dict[str, Any]:
    messages = _wire_messages(payload)
    system = _wire_system(payload, messages)
    components = _system_components(agent, system)
    msg_components, msg_hashes = _message_components(messages)
    components.update(msg_components)
    tools = payload.get("tools")
    components["tool_schemas"] = _tokens(tools) if tools else 0
    try:
        from agent.chat_completion_helpers import estimate_request_context_tokens
        local_total = int(estimate_request_context_tokens(payload) or 0)
    except Exception:
        local_total = sum(components.values())
    categorized = sum(components.values())
    if local_total > categorized: components["system"] += local_total - categorized
    elif categorized > local_total: local_total = categorized
    hashes = {"system": _sha(system), "tools": _sha(tools or []), "messages": msg_hashes}
    old_hashes = _row_hashes(_last_ledger_row(session_id))
    repeated = 0
    if old_hashes.get("system") == hashes["system"]:
        repeated += sum(components.get(k, 0) for k in ("system", "rules", "skills", "memory"))
    if old_hashes.get("tools") == hashes["tools"]: repeated += components["tool_schemas"]
    repeated += _common_prefix(msg_hashes, old_hashes.get("messages") or [])
    repeated = min(repeated, local_total)
    tool_msgs = [m for m in messages if _role(m) == "tool"]
    return {
        "components": components, "hashes": hashes, "local_total": local_total,
        "repeated": repeated, "unique": max(0, local_total - repeated),
        "tool_result_count": len(tool_msgs),
        "tool_result_bytes": sum(len(_content(m).encode("utf-8", errors="replace")) for m in tool_msgs),
        "tool_count": len(tools) if isinstance(tools, list) else 0,
        "tool_schema_bytes": len(_canon(tools).encode("utf-8")) if tools else 0,
    }


def _close_stale_pending(session_id: str, current_id: str) -> None:
    try:
        from agent.token_economy_store import finalize_token_request, pending_token_requests
        for row in pending_token_requests(session_id):
            if row.get("request_id") != current_id:
                finalize_token_request(str(row["request_id"]), status="retry_or_abandoned", error_type="no_response_before_next_attempt")
    except Exception:
        logger.debug("token-ledger stale-attempt close failed", exc_info=True)


def capture_request(agent: Any, payload: Dict[str, Any], *, api_request_id: str, turn_id: Any,
                    task_id: Any, api_call_index: int, retry_count: int,
                    local_deduplicated_tokens: int = 0) -> Optional[str]:
    settings = load_settings()
    if not settings.enabled or not settings.ledger_enabled or not getattr(agent, "session_id", None): return None
    ledger_id = f"{api_request_id}:try:{int(retry_count or 0)}"
    try:
        est = estimate_request_components(agent, payload, session_id=agent.session_id)
        _close_stale_pending(agent.session_id, ledger_id)
        ctx = int(getattr(getattr(agent, "context_compressor", None), "context_length", 0) or 0)
        from agent.token_economy_store import insert_token_request, maybe_prune
        # Retention maintenance is coordinated through the shared sidecar and runs
        # at most daily. Failure is telemetry-only and must never block inference.
        try:
            maybe_prune()
        except Exception:
            logger.debug("token-economy maintenance prune failed", exc_info=True)
        insert_token_request({
            "request_id": ledger_id, "session_id": agent.session_id,
            "turn_id": str(turn_id or ""), "task_id": str(task_id or ""),
            "api_call_index": int(api_call_index or 0), "retry_count": int(retry_count or 0),
            "fallback_attempt": int(retry_count or 0), "provider": str(getattr(agent, "provider", "") or ""),
            "model": str(getattr(agent, "model", "") or ""), "api_mode": str(getattr(agent, "api_mode", "") or ""),
            "created_at": time.time(), "status": "pending",
            "local_estimated_input_tokens": int(est["local_total"]), "local_estimated_unique_tokens": int(est["unique"]),
            "local_estimated_repeated_tokens": int(est["repeated"]),
            "local_deduplicated_tokens": int(local_deduplicated_tokens or 0),
            "lazy_schema_saved_tokens": int(getattr(agent, "_token_economy_lazy_schema_saved_tokens", 0) or 0),
            "tool_result_eviction_saved_tokens": int(local_deduplicated_tokens or 0),
            "compaction_saved_tokens": int(getattr(agent, "_token_economy_compaction_saved_tokens", 0) or 0),
            "context_max_tokens": ctx, "context_percent": (est["local_total"] / ctx * 100.0) if ctx else None,
            "tool_count": int(est["tool_count"]), "tool_schema_bytes": int(est["tool_schema_bytes"]),
            "retained_tool_result_count": int(est["tool_result_count"]), "retained_tool_result_bytes": int(est["tool_result_bytes"]),
            "component_estimates_json": _canon(est["components"]), "component_hashes_json": _canon(est["hashes"]),
            "compaction_reason": str(getattr(agent, "_token_economy_compaction_reason", "") or "") or None,
            "request_fingerprint": _sha({"system": est["hashes"]["system"], "tools": est["hashes"]["tools"],
                                         "messages": [m["hash"] for m in est["hashes"]["messages"]]}),
        })
        agent._token_economy_pending_request_id = ledger_id
        # Savings/reason apply to the first request after the compaction that produced them.
        agent._token_economy_compaction_saved_tokens = 0
        agent._token_economy_compaction_reason = ""
        return ledger_id
    except Exception:
        logger.debug("token request capture failed", exc_info=True)
        return None


def reconcile_response(agent: Any, usage: Any) -> None:
    request_id = getattr(agent, "_token_economy_pending_request_id", None)
    if not request_id: return
    try:
        from agent.token_economy_store import finalize_token_request, recent_token_requests
        row = next((r for r in recent_token_requests(agent.session_id, 8) if r.get("request_id") == request_id), None)
        local = int((row or {}).get("local_estimated_input_tokens") or 0)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        finalize_token_request(
            request_id, status="ok", provider_prompt_tokens=prompt,
            provider_input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            provider_output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
            reasoning_tokens=int(getattr(usage, "reasoning_tokens", 0) or 0),
            reconciliation_scale=(prompt/local if local and prompt else None),
            reconciliation_error_pct=(abs(local-prompt)/prompt*100.0 if prompt else None),
        )
    except Exception:
        logger.debug("token request reconciliation failed", exc_info=True)
    finally:
        agent._token_economy_pending_request_id = None


def finalize_no_usage(agent: Any, status: str = "ok_no_usage") -> None:
    request_id = getattr(agent, "_token_economy_pending_request_id", None)
    if not request_id: return
    try:
        from agent.token_economy_store import finalize_token_request
        finalize_token_request(request_id, status=status)
    except Exception: logger.debug("token request no-usage finalize failed", exc_info=True)
    finally: agent._token_economy_pending_request_id = None


def reconciled_components(row: Dict[str, Any]) -> Dict[str, int]:
    try: components = json.loads(row.get("component_estimates_json") or "{}")
    except Exception: components = {}
    scale = row.get("reconciliation_scale")
    if not isinstance(scale, (int, float)) or scale <= 0:
        return {str(k): int(v or 0) for k, v in components.items()}
    names = list(components)
    scaled = {str(k): max(0, round(int(components[k] or 0) * float(scale))) for k in names}
    target = int(row.get("provider_prompt_tokens") or 0)
    if target and names:
        key = "system" if "system" in scaled else names[0]
        scaled[key] = max(0, scaled[key] + target - sum(scaled.values()))
    return scaled


def _request_local_session_value(name: str, default: str = "") -> str:
    """Gateway ContextVar session metadata first, process environment second."""
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(name, default) or default)
    except Exception:
        return str(os.environ.get(name, default) or default)


def session_mode(agent: Any, settings: Optional[TokenEconomySettings] = None) -> str:
    settings = settings or load_settings()
    if settings.session_mode != "auto":
        return settings.session_mode
    source = _request_local_session_value("HERMES_SESSION_SOURCE", "").strip().lower()
    agent_platform = str(getattr(agent, "platform", "") or "").strip().lower()
    if (source in {"cron", "kanban", "subagent"}
            or agent_platform in {"cron", "kanban", "subagent"}
            or getattr(agent, "_kanban_task_id", None)):
        return "autonomous"
    request_platform = _request_local_session_value("HERMES_SESSION_PLATFORM", "").strip().lower()
    platform = request_platform or agent_platform or None
    try:
        from agent.coding_context import is_coding_context
        if is_coding_context(platform=platform):
            return "work"
    except Exception:
        pass
    return "short"


def configured_primary_provider(config: Any) -> str:
    """Return the configured main provider slug without resolving credentials/endpoints.

    This intentionally reads only non-secret routing metadata. A nested
    ``model.default.provider`` is accepted for older config shapes; explicit
    ``model.provider`` wins. Missing/unstructured config means normal auto-detect.
    """
    if not isinstance(config, dict):
        return "auto"
    model_cfg = config.get("model")
    if not isinstance(model_cfg, dict):
        return "auto"
    provider = str(model_cfg.get("provider") or "").strip().lower()
    if not provider:
        default = model_cfg.get("default")
        if isinstance(default, dict):
            provider = str(default.get("provider") or "").strip().lower()
    return provider or "auto"


def automatic_background_review_allowed() -> bool:
    """Stock behavior when token economy is off; opt-in only when it is on."""
    settings = load_settings()
    return not settings.enabled or settings.background_review_enabled


def mode_context_ceiling(agent: Any, settings: Optional[TokenEconomySettings] = None) -> int:
    settings = settings or load_settings()
    return {"short": settings.short_context_ceiling, "work": settings.work_context_ceiling,
            "autonomous": settings.autonomous_context_ceiling}[session_mode(agent, settings)]


def compact_task_projection(row: Optional[Dict[str, Any]], max_chars: int) -> str:
    state = row.get("state") if isinstance(row, dict) else None
    if not isinstance(state, dict) or not state: return ""
    compact = {"goal": state.get("goal", ""), "phase": state.get("phase", ""),
               "requirements": list(state.get("requirements") or [])[-8:], "todos": list(state.get("todos") or [])[-8:],
               "blockers": list(state.get("blockers") or [])[-5:], "completed": list(state.get("completed") or [])[-8:],
               "evidence": list(state.get("evidence") or [])[-8:], "next_action": state.get("next_action", "")}
    text = _canon(compact)
    if len(text) > max_chars:
        for key in ("requirements", "todos", "completed", "evidence"): compact[key] = compact[key][-4:]
        text = _canon(compact)
    if len(text) > max_chars: text = text[:max(0, max_chars-18)] + '"...truncated"}'
    return f"{_TASK_OPEN}\n{text}\n{_TASK_CLOSE}"


def _task_projection_epoch(messages: Sequence[Any]) -> str:
    """Stable id for the current compressed-history epoch, or ``""`` pre-compression.

    Durable task state is redundant while the full conversation is present. Once
    history is summarized, the summary bytes define an epoch. We pin one compact
    task-state projection for that epoch so tool/evidence updates do not rewrite an
    early user message on every API call and destroy provider prefix-cache reuse.
    """
    summaries = [_sha(message) for message in messages if _summary_message(message)]
    return _sha(summaries) if summaries else ""


def project_task_state(payload: Dict[str, Any], agent: Any) -> Dict[str, Any]:
    settings = load_settings()
    if not settings.enabled or not settings.task_state_enabled or session_mode(agent, settings) == "short":
        return payload
    key = "messages" if isinstance(payload.get("messages"), list) else "input" if isinstance(payload.get("input"), list) else None
    if key is None:
        return payload
    epoch = _task_projection_epoch(payload[key])
    if not epoch:
        # Full history still carries the live goal/evidence. Injecting task state here
        # is pure duplication and, because the state changes after tools, cache churn.
        return payload
    projection = ""
    if getattr(agent, "_token_economy_task_projection_epoch", "") == epoch:
        projection = str(getattr(agent, "_token_economy_task_projection_text", "") or "")
    if not projection:
        try:
            from agent.token_economy_store import get_task_state
            projection = compact_task_projection(get_task_state(agent.session_id), settings.task_state_projection_chars)
        except Exception:
            return payload
        if not projection:
            return payload
        agent._token_economy_task_projection_epoch = epoch
        agent._token_economy_task_projection_text = projection
    cloned = copy.deepcopy(payload)
    for msg in reversed(cloned[key]):
        if isinstance(msg, dict) and msg.get("role") == "user" and isinstance(msg.get("content"), str):
            if _TASK_OPEN not in msg["content"]:
                msg["content"] = msg["content"].rstrip() + "\n\n" + projection
            return cloned
    return payload


def _result_receipt(row: Dict[str, Any]) -> str:
    return (f"{_RESULT_OPEN}\nid={row.get('result_id')} tool={row.get('tool_name')} sha256={row.get('sha256')} "
            f"chars={int(row.get('char_count') or 0):,} tokens~{int(row.get('estimated_tokens') or 0):,}\n"
            f"{str(row.get('receipt') or '').strip()}\nUse tool_call with name=tool_result_read and result_id/offset/limit arguments for exact archived ranges.\n{_RESULT_CLOSE}")


def _tool_result_retention_cutoff(messages: Sequence[Any], retain_batches: int) -> int:
    """Index before which tool results are eligible for externalization.

    One *batch* is the tool-result set following an assistant message containing
    tool_calls. Keeping the newest batch verbatim preserves the evidence the model
    must reason over right now, while older batches from the SAME user turn become
    compact durable receipts instead of being resent on every subsequent tool loop.
    """
    retain_batches = max(0, int(retain_batches or 0))
    starts: List[int] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if (msg.get("role") == "assistant"
                and isinstance(msg.get("tool_calls"), list) and msg.get("tool_calls")):
            starts.append(i)
            continue
        # Responses API represents one assistant tool batch as one or more
        # consecutive function_call items followed by function_call_output items.
        if msg.get("type") == "function_call":
            previous_type = messages[i - 1].get("type") if i > 0 and isinstance(messages[i - 1], dict) else None
            if previous_type != "function_call":
                starts.append(i)
    if starts:
        if retain_batches <= 0:
            return len(messages)
        return starts[max(0, len(starts) - retain_batches)]
    # Legacy/provider-normalized transcripts without assistant tool_call markers:
    # retain results from the current user turn, but externalize older turns.
    return max((i for i, msg in enumerate(messages)
                if isinstance(msg, dict) and msg.get("role") == "user"), default=-1)


def externalize_historical_tool_results(payload: Dict[str, Any], agent: Any) -> Tuple[Dict[str, Any], int]:
    settings = load_settings()
    if not settings.enabled: return payload, 0
    key = "messages" if isinstance(payload.get("messages"), list) else "input" if isinstance(payload.get("input"), list) else None
    if key is None: return payload, 0
    seq = payload[key]
    retention_cutoff = _tool_result_retention_cutoff(seq, settings.retain_tool_result_turns)
    cloned, saved = None, 0
    try:
        from agent.token_economy_store import get_tool_result_for_call
        for i, msg in enumerate(seq):
            if i >= retention_cutoff or not isinstance(msg, dict) or _role(msg) != "tool":
                continue
            is_responses_output = msg.get("type") == "function_call_output"
            # Multimodal tool results are owned by the adapter/image-retirement path;
            # replacing an array wholesale would discard visual evidence.
            if is_responses_output:
                if not isinstance(msg.get("output"), str):
                    continue
            elif not isinstance(msg.get("content"), str):
                continue
            old = _content(msg)
            if len(old) < settings.tool_result_externalize_chars or _RESULT_OPEN in old:
                continue
            call_id = str((msg.get("call_id") if is_responses_output else msg.get("tool_call_id")) or "")
            row = get_tool_result_for_call(agent.session_id, call_id) if call_id else None
            if not row:
                continue
            receipt = _result_receipt(row)
            if len(receipt) >= len(old):
                continue
            if cloned is None:
                cloned = copy.deepcopy(payload)
            before = _tokens(msg)
            cloned[key][i]["output" if is_responses_output else "content"] = receipt
            saved += max(0, before - _tokens(cloned[key][i]))
    except Exception:
        logger.debug("historical result externalization failed", exc_info=True)
        return payload, 0
    return (cloned if cloned is not None else payload), saved


def request_waste_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    components = reconciled_components(row)
    prompt = int(row.get("provider_prompt_tokens") or row.get("local_estimated_input_tokens") or 0)
    scale = float(row.get("reconciliation_scale") or 1.0)
    repeated = min(prompt, round(int(row.get("local_estimated_repeated_tokens") or 0) * scale)) if prompt else 0
    return {"request_id": row.get("request_id"), "status": row.get("status"), "prompt_tokens": prompt,
            "components": components, "repeated_context_tokens": repeated,
            "repeated_context_percent": (repeated/prompt*100.0 if prompt else 0.0),
            "cache_read_tokens": int(row.get("cache_read_tokens") or 0), "cache_write_tokens": int(row.get("cache_write_tokens") or 0),
            "tool_schema_bytes": int(row.get("tool_schema_bytes") or 0),
            "retained_tool_result_bytes": int(row.get("retained_tool_result_bytes") or 0),
            "local_estimation_error_pct": row.get("reconciliation_error_pct")}


def _compact_excerpt(text: str, budget: int) -> str:
    clean = "\n".join(line.rstrip() for line in str(text).strip().splitlines() if line.strip())
    if len(clean) <= budget: return clean
    head = max(80, int(budget * 0.62))
    tail = max(60, budget - head - 28)
    return clean[:head].rstrip() + "\n... omitted ...\n" + clean[-tail:].lstrip()


def build_tool_result_receipt(tool_name: str, args: Any, content: str, *, is_error: bool,
                              max_chars: int) -> str:
    """Bounded, deterministic evidence receipt for a raw archived tool result."""
    args = args if isinstance(args, dict) else {}
    facts: Dict[str, Any] = {"status": "error" if is_error else "ok"}
    stripped = str(content or "").strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except Exception:
            data = None
        if isinstance(data, dict):
            for key in ("success", "status", "exit_code", "returncode", "error", "path", "full_output_path",
                        "count", "url", "title", "session_id"):
                value = data.get(key)
                if value is not None and not isinstance(value, (dict, list)):
                    facts[key] = str(value)[:240]
    for key in ("path", "workdir", "query", "url"):
        value = args.get(key)
        if value is not None:
            facts[f"arg_{key}"] = str(value)[:220]
    if tool_name == "terminal" and args.get("command"):
        facts["command"] = str(args.get("command"))[:320]
    prefix = _canon(facts)
    remain = max(80, max_chars - len(prefix) - 10)
    excerpt = _compact_excerpt(stripped, remain)
    return (prefix + ("\n" + excerpt if excerpt else ""))[:max_chars]


def _persistence_class(tool_name: str, args: Dict[str, Any], content: str, is_error: bool) -> str:
    if is_error: return "blocker"
    if tool_name in {"write_file", "patch", "kanban_complete", "kanban_request_review"}: return "durable_evidence"
    if tool_name == "terminal":
        command = str(args.get("command") or "").lower()
        if any(word in command for word in ("pytest", "unittest", "npm test", "pnpm test", "cargo test", "go test", "git commit", "git status")):
            return "durable_evidence"
    if tool_name.startswith("web_") or tool_name.startswith("browser_"): return "reference"
    return "working"


def archive_tool_result_for_agent(agent: Any, *, tool_name: str, tool_call_id: str,
                                  args: Any, content: Any, is_error: bool = False) -> Optional[Dict[str, Any]]:
    settings = load_settings()
    if not settings.enabled or not isinstance(content, str) or not getattr(agent, "session_id", None): return None
    try:
        from agent.token_economy_store import archive_tool_result
        arg_dict = args if isinstance(args, dict) else {}
        receipt = build_tool_result_receipt(tool_name, arg_dict, content, is_error=is_error,
                                            max_chars=settings.tool_result_receipt_chars)
        archived = archive_tool_result(
            session_id=agent.session_id, turn_id=str(getattr(agent, "_current_turn_id", "") or ""),
            tool_call_id=str(tool_call_id or ""), tool_name=tool_name, content=content, receipt=receipt,
            persistence_class=_persistence_class(tool_name, arg_dict, content, is_error), is_error=is_error,
            metadata={"arg_keys": sorted(str(k) for k in arg_dict)[:40]},
        )
        note_tool_result(agent, archived, is_error=is_error)
        return archived
    except Exception:
        logger.debug("raw tool-result archive failed", exc_info=True)
        return None


def _load_state(session_id: str) -> Dict[str, Any]:
    try:
        from agent.token_economy_store import get_task_state
        row = get_task_state(session_id)
        return dict((row or {}).get("state") or {})
    except Exception:
        return {}


def _save_state(session_id: str, state: Dict[str, Any]) -> None:
    try:
        from agent.token_economy_store import upsert_task_state
        upsert_task_state(session_id, state)
    except Exception:
        logger.debug("automatic task-state save failed", exc_info=True)


def _append_unique(state: Dict[str, Any], key: str, value: str, limit: int = 30) -> None:
    value = str(value or "").strip()
    if not value: return
    items = list(state.get(key) or [])
    if value not in items: items.append(value)
    state[key] = items[-limit:]


def note_user_turn(agent: Any, user_message: Any) -> None:
    settings = load_settings()
    sid = str(getattr(agent, "session_id", "") or "")
    if not settings.enabled or not settings.task_state_enabled or not sid: return
    text = str(user_message or "").strip()
    if not text: return
    state = _load_state(sid)
    # First substantive instruction becomes the durable goal. Later user turns are
    # requirements only when they carry enough content to be useful after compaction.
    if not state.get("goal") and len(text) >= 12:
        state["goal"] = text[:1400]
    elif len(text) >= 24 and text[:600] != state.get("goal"):
        _append_unique(state, "requirements", text[:700], 20)
    state["phase"] = "executing"
    state["next_action"] = "Continue the current user request from durable evidence and the live transcript."
    _save_state(sid, state)


def note_tool_result(agent: Any, archived: Optional[Dict[str, Any]], *, is_error: bool) -> None:
    settings = load_settings()
    sid = str(getattr(agent, "session_id", "") or "")
    if not settings.enabled or not settings.task_state_enabled or not sid or not archived: return
    state = _load_state(sid)
    rid, tool = str(archived.get("result_id") or ""), str(archived.get("tool_name") or "tool")
    evidence = f"{tool}: {rid} ({archived.get('persistence_class')}, sha256={str(archived.get('sha256') or '')[:12]})"
    _append_unique(state, "evidence", evidence, 24)
    if is_error:
        state["phase"] = "blocked"
        _append_unique(state, "blockers", f"{tool} failed; inspect {rid} for exact output", 12)
    else:
        state["phase"] = "executing"
        if archived.get("persistence_class") == "durable_evidence":
            _append_unique(state, "completed", f"{tool} evidence captured as {rid}", 20)
    _save_state(sid, state)


def note_turn_finalized(agent: Any, *, failed: bool, interrupted: bool, exit_reason: Any) -> None:
    settings = load_settings()
    sid = str(getattr(agent, "session_id", "") or "")
    if not settings.enabled or not settings.task_state_enabled or not sid: return
    state = _load_state(sid)
    if not state: return
    state["phase"] = "blocked" if failed else "interrupted" if interrupted else "awaiting_user"
    if failed:
        _append_unique(state, "blockers", f"turn ended: {str(exit_reason or 'failed')[:240]}", 12)
    state["next_action"] = "Resume from durable task state and evidence on the next turn."
    _save_state(sid, state)


def apply_compressor_policy(agent: Any) -> bool:
    """Apply lifetime caps to the built-in compressor; leave >=35% reserve."""
    settings = load_settings()
    if not settings.enabled: return False
    try:
        from agent.context_compressor import ContextCompressor
        compressor = getattr(agent, "context_compressor", None)
        if not isinstance(compressor, ContextCompressor): return False
        mode = session_mode(agent, settings)
        mode_cap = mode_context_ceiling(agent, settings)
        context_length = int(compressor.context_length or 0)
        reserve_cap = max(8000, int(context_length * 0.65)) if context_length else mode_cap
        cap = min(mode_cap, reserve_cap)
        tail_budget, summary_min, summary_max = {
            "short": (2500, 512, 768),
            "work": (5000, 768, 1536),
            "autonomous": (7000, 1024, 2048),
        }[mode]
        compressor._runtime_tail_token_budget_override = min(tail_budget, max(1, cap - 1))
        compressor._runtime_min_summary_tokens_override = summary_min
        compressor._runtime_max_summary_tokens_override = summary_max
        existing = getattr(compressor, "threshold_tokens_cap", None)
        if isinstance(existing, int) and existing > 0: cap = min(cap, existing)
        compressor.threshold_tokens_cap = cap
        compressor._threshold_tokens = None
        compressor._tail_token_budget = None
        compressor._max_summary_tokens = None
        trigger = max(6000, int(cap * 2 / 3))
        old_trigger = int(getattr(compressor, "proactive_prune_tokens", 0) or 0)
        compressor.proactive_prune_tokens = min(old_trigger, trigger) if old_trigger > 0 else trigger
        compressor.proactive_prune_min_result_chars = min(
            int(getattr(compressor, "proactive_prune_min_result_chars", 8000) or 8000), 2000)
        current_min_reclaim = int(getattr(compressor, "proactive_prune_min_reclaim_tokens", 4096) or 4096)
        compressor.proactive_prune_min_reclaim_tokens = min(
            current_min_reclaim, max(1024, cap // 4))
        logger.info(
            "token economy compressor policy: mode=%s cap=%s prune=%s tail=%s summary=%s..%s reserve>=35%%",
            mode, cap, compressor.proactive_prune_tokens, compressor.tail_token_budget,
            compressor._runtime_min_summary_tokens_override, compressor.max_summary_tokens,
        )
        return True
    except Exception:
        logger.debug("token-economy compressor policy failed", exc_info=True)
        return False


def note_compaction(agent: Any, before_messages: Sequence[Any], after_messages: Sequence[Any], *, reason: str) -> int:
    """Record local compaction savings and persist the newest summary with provenance."""
    if not load_settings().enabled or before_messages is after_messages: return 0
    before = sum(_tokens(m) for m in before_messages)
    after = sum(_tokens(m) for m in after_messages)
    saved = max(0, before - after)
    agent._token_economy_compaction_saved_tokens = int(
        getattr(agent, "_token_economy_compaction_saved_tokens", 0) or 0) + saved
    agent._token_economy_compaction_reason = str(reason or "compaction")
    sid = str(getattr(agent, "session_id", "") or "")
    if sid:
        summary_text = ""
        for msg in after_messages:
            if _summary_message(msg):
                candidate = _content(msg)
                if candidate: summary_text = candidate
        if summary_text:
            try:
                from agent.token_economy_store import add_summary
                add_summary(sid, summary_text, {
                    "reason": str(reason or "compaction"),
                    "turn_id": str(getattr(agent, "_current_turn_id", "") or ""),
                    "before_estimated_tokens": before, "after_estimated_tokens": after,
                    "estimated_saved_tokens": saved,
                })
            except Exception:
                logger.debug("summary provenance persistence failed", exc_info=True)
    return saved


def _scaled_repeated(row: Dict[str, Any]) -> int:
    raw = int(row.get("local_estimated_repeated_tokens") or 0)
    scale = float(row.get("reconciliation_scale") or 1.0)
    prompt = int(row.get("provider_prompt_tokens") or row.get("local_estimated_input_tokens") or 0)
    return min(prompt, max(0, round(raw * scale))) if prompt else max(0, round(raw * scale))


def _row_prompt(row: Dict[str, Any]) -> int:
    return int(row.get("provider_prompt_tokens") or row.get("local_estimated_input_tokens") or 0)


def context_waste_lines(session_id: str) -> List[str]:
    from agent.token_economy_store import recent_token_requests
    rows = recent_token_requests(session_id, 1)
    if not rows:
        return ["Token Ledger has no request rows for this session yet. Send one model turn first."]
    row = rows[0]
    prompt = _row_prompt(row)
    measured = row.get("provider_prompt_tokens") is not None
    components = reconciled_components(row)
    lines = [
        f"Request: {row.get('request_id')}",
        f"Input: {'provider-measured' if measured else 'local estimate'} {prompt:,} tokens",
        "Ranked request contributors:",
    ]
    for name, amount in sorted(components.items(), key=lambda kv: kv[1], reverse=True):
        pct = (amount / prompt * 100.0) if prompt else 0.0
        lines.append(f"  {name:<24} {amount:>9,}  {pct:>5.1f}%")
    repeated = _scaled_repeated(row)
    lines.extend([
        "",
        f"Repeated prefix estimate:      {repeated:>9,} tokens ({repeated / prompt * 100.0 if prompt else 0.0:.1f}%)",
        f"Provider cache read:           {int(row.get('cache_read_tokens') or 0):>9,} tokens",
        f"Lazy-schema saved (modeled):   {int(row.get('lazy_schema_saved_tokens') or 0):>9,} tokens",
        f"Tool-result eviction saved:    {int(row.get('tool_result_eviction_saved_tokens') or 0):>9,} tokens",
        f"Compaction saved (estimated):  {int(row.get('compaction_saved_tokens') or 0):>9,} tokens",
    ])
    if row.get("compaction_reason"):
        lines.append(f"Compaction reason: {row['compaction_reason']}")
    if measured and row.get("reconciliation_error_pct") is not None:
        err = float(row["reconciliation_error_pct"])
        lines.append(f"Local-estimator reconciliation error: {err:.2f}% ({'PASS <=5%' if err <= 5.0 else 'OUTSIDE <=5% gate'})")
    return lines


def context_diff_lines(session_id: str) -> List[str]:
    from agent.token_economy_store import recent_token_requests
    rows = recent_token_requests(session_id, 2)
    if len(rows) < 2:
        return ["Need at least two Token Ledger requests in this session for /context diff."]
    cur, prev = rows[0], rows[1]
    cur_c, prev_c = reconciled_components(cur), reconciled_components(prev)
    names = sorted(set(cur_c) | set(prev_c), key=lambda n: cur_c.get(n, 0) - prev_c.get(n, 0), reverse=True)
    lines = [
        f"Input delta: {_row_prompt(cur) - _row_prompt(prev):+,} tokens ({_row_prompt(prev):,} -> {_row_prompt(cur):,})",
        f"Repeated-prefix delta: {_scaled_repeated(cur) - _scaled_repeated(prev):+,} tokens",
        "Category deltas:",
    ]
    for name in names:
        delta = int(cur_c.get(name, 0)) - int(prev_c.get(name, 0))
        if delta:
            lines.append(f"  {name:<24} {delta:>+9,}")
    lines.append(f"  tool_schema_bytes          {int(cur.get('tool_schema_bytes') or 0)-int(prev.get('tool_schema_bytes') or 0):>+9,} bytes")
    lines.append(f"  retained_tool_result_bytes {int(cur.get('retained_tool_result_bytes') or 0)-int(prev.get('retained_tool_result_bytes') or 0):>+9,} bytes")
    return lines


def context_history_lines(session_id: str, limit: int = 12) -> List[str]:
    from agent.token_economy_store import recent_token_requests
    rows = list(reversed(recent_token_requests(session_id, max(1, min(int(limit or 12), 50)))))
    if not rows:
        return ["Token Ledger has no request rows for this session yet."]
    lines = ["idx  input      output   repeat    cache-r   schemas  results status"]
    for idx, row in enumerate(rows, 1):
        lines.append(
            f"{idx:>3}  {_row_prompt(row):>9,}  {int(row.get('provider_output_tokens') or 0):>7,}  "
            f"{_scaled_repeated(row):>8,}  {int(row.get('cache_read_tokens') or 0):>8,}  "
            f"{int(row.get('tool_schema_bytes') or 0)//4:>7,}  {int(row.get('retained_tool_result_bytes') or 0)//4:>7,} "
            f"{str(row.get('status') or '')}"
        )
    return lines


def waste_insights_lines(days: int = 30) -> List[str]:
    from agent.token_economy_store import token_requests_since
    days = max(1, min(int(days or 30), 3650))
    rows = token_requests_since(time.time() - days * 86400)
    if not rows:
        return [f"No Token Ledger rows in the last {days} day(s)."]
    gross = repeated = schema = tool_carry = sys_rules = cache_r = cache_w = output = 0
    lazy_saved = result_saved = compact_saved = 0
    provider_rows = 0
    by_session: Dict[str, Dict[str, int]] = {}
    for row in rows:
        prompt = _row_prompt(row)
        gross += prompt
        output += int(row.get("provider_output_tokens") or 0)
        rep = _scaled_repeated(row)
        repeated += rep
        comps = reconciled_components(row)
        schema += int(comps.get("tool_schemas", 0))
        tool_carry += int(comps.get("retained_tool_results", 0))
        sys_rules += int(comps.get("system", 0)) + int(comps.get("rules", 0))
        cache_r += int(row.get("cache_read_tokens") or 0)
        cache_w += int(row.get("cache_write_tokens") or 0)
        lazy = int(row.get("lazy_schema_saved_tokens") or 0)
        result = int(row.get("tool_result_eviction_saved_tokens") or 0)
        compact = int(row.get("compaction_saved_tokens") or 0)
        lazy_saved += lazy; result_saved += result; compact_saved += compact
        if row.get("provider_prompt_tokens") is not None: provider_rows += 1
        sid = str(row.get("session_id") or "unknown")
        bucket = by_session.setdefault(sid, {"gross": 0, "avoidable": 0, "requests": 0})
        bucket["gross"] += prompt; bucket["avoidable"] += rep + result + compact + lazy; bucket["requests"] += 1
    useful = max(0, gross - repeated)
    hit_ratio = (cache_r / gross * 100.0) if gross else 0.0
    lines = [
        f"Token Waste Insights - last {days} day(s)",
        f"Requests:                     {len(rows):>12,} ({provider_rows:,} provider-reconciled)",
        f"Gross model input:            {gross:>12,} tokens",
        f"Net working-set estimate:     {useful:>12,} tokens",
        f"Repeated-history estimate:    {repeated:>12,} tokens ({repeated/gross*100.0 if gross else 0.0:.1f}%)",
        f"Tool-schema input:            {schema:>12,} tokens",
        f"Raw/receipt tool-result carry:{tool_carry:>12,} tokens",
        f"System + rules:               {sys_rules:>12,} tokens",
        f"Provider cache read/write:    {cache_r:>12,} / {cache_w:,} ({hit_ratio:.1f}% read/gross)",
        f"Model output:                 {output:>12,} tokens",
        "",
        f"Lazy-schema savings*:         {lazy_saved:>12,} tokens",
        f"Tool-result eviction saved:   {result_saved:>12,} tokens",
        f"Compaction savings*:          {compact_saved:>12,} tokens",
        "* counterfactual/local-estimate metric; not provider billing telemetry",
        "Tokens per successful task: n/a until explicit success-outcome telemetry is recorded.",
        "",
        "Top sessions by estimated avoidable resend:",
    ]
    ranked = sorted(by_session.items(), key=lambda kv: kv[1]["avoidable"], reverse=True)[:10]
    for sid, data in ranked:
        lines.append(f"  {sid[:30]:<30} avoid~{data['avoidable']:>10,}  gross={data['gross']:>10,}  req={data['requests']}")
    return lines
