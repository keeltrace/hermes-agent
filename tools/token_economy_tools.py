"""Deferred recovery/state tools for token-economy mode."""
from __future__ import annotations

import json
from typing import Any, Dict

from tools.registry import registry, tool_error


def _enabled() -> bool:
    try:
        from agent.token_economy import load_settings
        return load_settings().enabled
    except Exception:
        return False


def _session_id() -> str:
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env("HERMES_SESSION_ID", "") or "").strip()
    except Exception:
        return ""


def _tool_result_read(args: Dict[str, Any], **_kw) -> str:
    result_id = str(args.get("result_id") or "").strip()
    if not result_id:
        return tool_error("result_id is required")
    sid = _session_id()
    if not sid:
        return tool_error("No active Hermes session id is available for result recovery.")
    from agent.token_economy_store import get_tool_result, read_tool_result
    meta = get_tool_result(result_id)
    if not meta or str(meta.get("session_id") or "") != sid:
        return tool_error("Tool result is unavailable in this session.")
    result = read_tool_result(result_id, offset=args.get("offset", 1), limit=args.get("limit", 200))
    return json.dumps(result, ensure_ascii=False)


TOOL_RESULT_READ_SCHEMA = {
    "name": "tool_result_read",
    "description": (
        "Read an exact line range from a raw tool result archived by Hermes. Use only when a "
        "<tool-result-ref> receipt says the historical body was externalized; do not re-run the original tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "result_id": {"type": "string", "description": "Opaque tr_... id from a tool-result receipt."},
            "offset": {"type": "integer", "minimum": 1, "default": 1, "description": "1-based starting line."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200, "description": "Maximum lines."},
        },
        "required": ["result_id"],
    },
}

_STATE_FIELDS = frozenset({"goal", "phase", "requirements", "todos", "blockers", "completed", "evidence", "next_action"})
_LIST_FIELDS = frozenset({"requirements", "todos", "blockers", "completed", "evidence"})


def _clean_state(value: Any) -> Dict[str, Any]:
    state = value if isinstance(value, dict) else {}
    out: Dict[str, Any] = {}
    for key, item in state.items():
        if key not in _STATE_FIELDS:
            continue
        if key in _LIST_FIELDS:
            vals = item if isinstance(item, list) else [item]
            out[key] = [str(v)[:600] for v in vals if str(v).strip()][-40:]
        else:
            out[key] = str(item or "")[:4000]
    return out


def _merge_state(current: Dict[str, Any], patch: Dict[str, Any], *, append: bool) -> Dict[str, Any]:
    merged = _clean_state(current)
    patch = _clean_state(patch)
    for key, value in patch.items():
        if append and key in _LIST_FIELDS:
            prior = list(merged.get(key) or [])
            for entry in value:
                if entry not in prior:
                    prior.append(entry)
            merged[key] = prior[-40:]
        else:
            merged[key] = value
    return merged


def _task_state(args: Dict[str, Any], **_kw) -> str:
    sid = _session_id()
    if not sid:
        return tool_error("No active Hermes session id is available for task state.")
    from agent.token_economy_store import get_task_state, upsert_task_state
    action = str(args.get("action") or "get").strip().lower()
    existing = get_task_state(sid)
    current = (existing or {}).get("state") or {}
    if action == "get":
        return json.dumps(existing or {"session_id": sid, "revision": 0, "state": {}}, ensure_ascii=False)
    patch = args.get("state")
    if not isinstance(patch, dict):
        return tool_error("state must be an object for merge, append, or replace.")
    if action == "replace":
        state = _clean_state(patch)
    elif action in {"merge", "append"}:
        state = _merge_state(current, patch, append=(action == "append"))
    else:
        return tool_error("action must be get, merge, append, or replace.")
    saved = upsert_task_state(sid, state)
    return json.dumps(saved, ensure_ascii=False)


TASK_STATE_SCHEMA = {
    "name": "task_state",
    "description": (
        "Read or checkpoint durable structured task state outside chat history. Use for long work when "
        "requirements, blockers, evidence, completed items, or next action must survive compaction/restart."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "merge", "append", "replace"], "default": "get"},
            "state": {
                "type": "object",
                "description": "Fields: goal, phase, requirements[], todos[], blockers[], completed[], evidence[], next_action.",
                "additionalProperties": True,
            },
        },
        "required": [],
    },
}


registry.register(name="tool_result_read", toolset="token_economy", schema=TOOL_RESULT_READ_SCHEMA,
                  handler=_tool_result_read, check_fn=_enabled, emoji="🧾", max_result_size_chars=100_000)
registry.register(name="task_state", toolset="token_economy", schema=TASK_STATE_SCHEMA,
                  handler=_task_state, check_fn=_enabled, emoji="🧭", max_result_size_chars=40_000)
