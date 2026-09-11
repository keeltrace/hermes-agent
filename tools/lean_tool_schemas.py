"""Compact model-visible schemas for the always-on coding tool waist.

The dispatcher still receives the exact same JSON-schema structure: this module
only replaces descriptive prose for a small, curated set of high-frequency
core tools.  Full original schemas remain available through ``tool_describe``
and through callers that request raw definitions, so progressive disclosure can
recover every nuance without paying for it on every model turn.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Mapping


_DIRECT_SCHEMA_COPY: Mapping[str, Mapping[str, Any]] = {
    "clarify": {
        "description": "Ask the user for a necessary decision or clarification. Batch independent questions; put clickable options only in choices, recommended first. Omit choices for free text. Do not use for terminal safety confirmation.",
        "properties": {
            "questions": "Questions to ask in order; each has question text, optional choices, and optional multi_select.",
        },
    },
    "terminal": {
        "description": "Run a shell command. Use background=true for long-lived work; notify for bounded background completion.",
        "properties": {
            "command": "Shell command to execute.",
            "background": "Run in background and return a session id.",
            "timeout": "Max foreground wait in seconds; returns sooner when finished.",
            "workdir": "Absolute working directory; defaults to session cwd.",
            "pty": "Allocate a PTY for interactive background commands.",
            "notify": "Background only: true=notify on exit; string list=notify on matching output.",
        },
    },
    "read_file": {
        "description": "Read a file with line numbers and pagination; supported documents are text-extracted automatically.",
        "properties": {
            "path": "File path.",
            "offset": "1-based starting line.",
            "limit": "Maximum lines to return.",
        },
    },
    "write_file": {
        "description": "Replace a file with complete content, creating parent directories. Use patch for targeted edits.",
        "properties": {
            "path": "File path.",
            "content": "Complete file content.",
        },
    },
    "patch": {
        "description": "Edit files by targeted replacement or the advertised patch mode. Returns a diff and validates syntax.",
        "properties": {
            "path": "File path for replacement mode.",
            "old_string": "Text to replace; unique unless replace_all=true.",
            "new_string": "Replacement text; empty deletes the match.",
            "replace_all": "Replace every occurrence.",
            "mode": "Edit mode: replace or patch.",
            "patch": "Patch payload when mode=patch.",
        },
    },
    "search_files": {
        "description": "Search file contents by regex or find files by glob, with bounded pagination.",
        "properties": {
            "pattern": "Regex for content search or glob for file search.",
            "target": "Search file content or file names.",
            "path": "Directory or file; defaults to cwd.",
            "file_glob": "Optional file filter for content search.",
            "limit": "Maximum results.",
            "offset": "Results to skip.",
            "order": "File-search ordering.",
            "output_mode": "Content, paths only, or counts.",
            "context": "Context lines around content matches.",
        },
    },
}

LEAN_DIRECT_TOOL_NAMES = frozenset(_DIRECT_SCHEMA_COPY)


def _function_node(tool_def: Dict[str, Any]) -> Dict[str, Any]:
    node = tool_def.get("function")
    return node if isinstance(node, dict) else tool_def


def compact_direct_tool_schemas(tool_defs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return copied tool definitions with only curated descriptions shortened.

    All non-description keys (types, required lists, enums, defaults, bounds,
    dynamic schema extensions, etc.) are preserved byte-for-byte at the Python
    object level. Unknown tools and unknown parameters are left untouched.
    """
    out: List[Dict[str, Any]] = []
    for original in tool_defs:
        fn = _function_node(original)
        name = str(fn.get("name") or "")
        profile = _DIRECT_SCHEMA_COPY.get(name)
        if profile is None:
            out.append(original)
            continue

        td = copy.deepcopy(original)
        compact_fn = _function_node(td)
        compact_fn["description"] = profile["description"]
        parameters = compact_fn.get("parameters")
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        if isinstance(properties, dict):
            replacements = profile.get("properties", {})
            for prop_name, concise in replacements.items():
                prop = properties.get(prop_name)
                if isinstance(prop, dict) and "description" in prop:
                    prop["description"] = concise
        out.append(td)
    return out
