"""Loaders for the frozen semantic-layer contract (YAML definitions and their schema)."""

import json
from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).parent
SEMANTIC_LAYER_PATH = PACKAGE_DIR / "semantic_layer.yaml"
SEMANTIC_LAYER_SCHEMA_PATH = PACKAGE_DIR / "semantic_layer.schema.json"


def load_semantic_layer() -> dict[str, Any]:
    """Return the parsed semantic layer definitions."""
    data = yaml.safe_load(SEMANTIC_LAYER_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("semantic_layer.yaml must contain a mapping")
    return data


def load_semantic_layer_schema() -> dict[str, Any]:
    """Return the JSON Schema that validates semantic_layer.yaml."""
    data = json.loads(SEMANTIC_LAYER_SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("semantic_layer.schema.json must contain a JSON object")
    return data
