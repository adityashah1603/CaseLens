"""Load the frozen semantic layer once, validate it, and cross-check its references."""

from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

from caselens_semantic_layer.contracts import load_semantic_layer, load_semantic_layer_schema

# Names the compiler binds itself; an operation param may never use them.
RESERVED_PARAMS = frozenset({"case_id"})


class LayerError(RuntimeError):
    """The semantic layer contract is internally inconsistent (a deployment bug, not user input)."""


def _predicates(filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in filters:
        if "any_of" in item:
            for branch in item["any_of"]:
                out.extend(branch.get("all_of", [branch]))
        elif "all_of" in item:
            out.extend(item["all_of"])
        else:
            out.append(item)
    return out


def _check_operation(layer: dict[str, Any], name: str, op: dict[str, Any]) -> None:
    sources = layer["sources"]
    if op["source"] not in sources:
        raise LayerError(f"operation {name}: unknown source")
    source = sources[op["source"]]
    columns = source["columns"]
    if source["citation"] is None:
        raise LayerError(f"operation {name}: source rows are not citable")
    for param_name, param in op["params"].items():
        if param_name in RESERVED_PARAMS:
            raise LayerError(f"operation {name}: param name {param_name} is reserved")
        if param["type"] not in layer["param_types"]:
            raise LayerError(f"operation {name}: unknown param type for {param_name}")
    for pred in _predicates(op["filters"]):
        column = pred["column"]
        if column not in columns or column == source["case_column"]:
            raise LayerError(f"operation {name}: bad filter column {column}")
        bound = pred["params"] if "params" in pred else [pred["param"]]
        for param_name in bound:
            if param_name not in op["params"]:
                raise LayerError(f"operation {name}: undeclared param {param_name}")
            col_type = columns[column].get("param_type")
            if col_type is not None and col_type != op["params"][param_name]["type"]:
                raise LayerError(f"operation {name}: param {param_name} type mismatch")
    for column in op.get("select", []):
        if column not in columns:
            raise LayerError(f"operation {name}: unknown select column {column}")
    for order in op.get("order_by", []):
        if order["column"] not in columns:
            raise LayerError(f"operation {name}: unknown order_by column")
    if "metric" in op:
        metric = layer["metrics"].get(op["metric"])
        if metric is None or metric["source"] != op["source"]:
            raise LayerError(f"operation {name}: bad metric")
        if "column" in metric and metric["column"] not in columns:
            raise LayerError(f"operation {name}: unknown metric column")


@lru_cache(maxsize=1)
def get_layer() -> dict[str, Any]:
    """Return the semantic layer, validated against its JSON Schema and cross-checked (cached)."""
    layer = load_semantic_layer()
    schema = load_semantic_layer_schema()
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(layer)
    for source in layer["sources"].values():
        cols = source["columns"]
        if source["primary_key"] not in cols or source["case_column"] not in cols:
            raise LayerError(f"source {source['table']}: key columns missing")
        citation = source["citation"]
        if citation is not None:
            if citation["table"] != source["table"]:
                raise LayerError(f"source {source['table']}: citation table mismatch")
            for key in ("record_id_column", "evidence_id_column", "row_id_column"):
                if citation[key] not in cols:
                    raise LayerError(f"source {source['table']}: citation column missing")
    for name, op in layer["operations"].items():
        _check_operation(layer, name, op)
    return layer


@lru_cache(maxsize=1)
def get_tool_validator() -> Draft202012Validator:
    """Validator for the frozen `query_records` tool input_schema (cached)."""
    from caselens_agent.contracts import load_tool_definitions

    schema = load_tool_definitions()["query_records"]["input_schema"]
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
