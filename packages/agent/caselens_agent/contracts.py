"""Loaders for the frozen agent contracts (answer schema and tool definitions)."""

import json
from pathlib import Path
from typing import Any

SCHEMAS_DIR = Path(__file__).parent / "schemas"
TOOLS_DIR = SCHEMAS_DIR / "tools"


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def load_answer_schema() -> dict[str, Any]:
    """Return the answer-contract JSON Schema (draft 2020-12)."""
    return _load_json(SCHEMAS_DIR / "answer.schema.json")


def load_tool_definition_schema() -> dict[str, Any]:
    """Return the meta-schema every tool definition file must satisfy."""
    return _load_json(SCHEMAS_DIR / "tool_definition.schema.json")


def load_tool_definitions() -> dict[str, dict[str, Any]]:
    """Return every tool definition keyed by tool name, in file-name order."""
    tools: dict[str, dict[str, Any]] = {}
    for path in sorted(TOOLS_DIR.glob("*.json")):
        tool = _load_json(path)
        tools[str(tool["name"])] = tool
    return tools
