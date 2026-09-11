from __future__ import annotations

import copy
import json

from tools.lean_tool_schemas import LEAN_DIRECT_TOOL_NAMES, compact_direct_tool_schemas


def _schema(name: str, description: str = "very long top-level description") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "a very long path description that should shrink",
                        "minLength": 1,
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["replace", "patch"],
                        "description": "a very long mode description that should shrink",
                        "default": "replace",
                    },
                    "future_parameter": {
                        "type": "integer",
                        "description": "unknown future parameters must retain their teaching text",
                        "minimum": 0,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }


def _without_descriptions(value):
    if isinstance(value, dict):
        return {k: _without_descriptions(v) for k, v in value.items() if k != "description"}
    if isinstance(value, list):
        return [_without_descriptions(v) for v in value]
    return value


def test_compaction_preserves_every_non_description_schema_contract():
    original = _schema("patch")
    compact = compact_direct_tool_schemas([original])[0]
    assert _without_descriptions(compact) == _without_descriptions(original)
    assert original["function"]["description"] == "very long top-level description"
    assert compact is not original


def test_unknown_parameters_keep_full_description():
    compact = compact_direct_tool_schemas([_schema("patch")])[0]
    prop = compact["function"]["parameters"]["properties"]["future_parameter"]
    assert prop["description"] == "unknown future parameters must retain their teaching text"


def test_unknown_tools_are_not_copied_or_rewritten():
    original = _schema("clarify")
    assert compact_direct_tool_schemas([original])[0] is original


def test_curated_direct_waist_is_intentionally_small():
    assert LEAN_DIRECT_TOOL_NAMES == {"clarify", "terminal", "read_file", "write_file", "patch", "search_files"}


def test_compaction_reduces_representative_direct_schema_bytes():
    verbose = _schema("patch", "word " * 300)
    verbose["function"]["parameters"]["properties"]["path"]["description"] = "path detail " * 100
    before = len(json.dumps(verbose, separators=(",", ":")))
    after = len(json.dumps(compact_direct_tool_schemas([verbose])[0], separators=(",", ":")))
    assert after < before * 0.45


def test_clarify_compaction_preserves_interactive_contract():
    original = {
        "type": "function",
        "function": {
            "name": "clarify",
            "description": "long guidance " * 100,
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "description": "long question guidance " * 60,
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
                                "choices": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                                "multi_select": {"type": "boolean"},
                            },
                            "required": ["question"],
                        },
                    },
                },
                "required": ["questions"],
            },
        },
    }
    compact = compact_direct_tool_schemas([original])[0]
    assert _without_descriptions(compact) == _without_descriptions(original)
    assert len(json.dumps(compact)) < len(json.dumps(original)) * 0.35
