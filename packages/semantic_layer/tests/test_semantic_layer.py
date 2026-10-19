"""Contract tests for semantic_layer.yaml (SYNTHETIC definitions only)."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from caselens_semantic_layer.contracts import load_semantic_layer, load_semantic_layer_schema

REPO_ROOT = Path(__file__).resolve().parents[3]
QUERY_RECORDS_TOOL = REPO_ROOT / "packages/agent/caselens_agent/schemas/tools/query_records.json"
ALLOWED_TABLES = {"call_records", "plate_reads", "custody_records", "entities"}


@pytest.fixture(scope="module")
def layer() -> dict[str, Any]:
    return load_semantic_layer()


def _predicates(filters: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for item in filters:
        if "any_of" in item:
            for branch in item["any_of"]:
                yield from branch.get("all_of", [branch])
        else:
            yield item


def _filter_params(pred: dict[str, Any]) -> list[str]:
    return list(pred["params"]) if "params" in pred else [pred["param"]]


def test_schema_is_valid_and_yaml_validates(layer: dict[str, Any]) -> None:
    schema = load_semantic_layer_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(layer)


def test_defaults(layer: dict[str, Any]) -> None:
    assert layer["defaults"] == {
        "role": "caselens_readonly",
        "max_rows": 500,
        "statement_timeout_ms": 5000,
    }


def test_source_tables_are_allowed(layer: dict[str, Any]) -> None:
    for source in layer["sources"].values():
        assert source["table"] in ALLOWED_TABLES
        cols = source["columns"]
        assert source["primary_key"] in cols
        assert source["case_column"] in cols
        citation = source["citation"]
        if citation is not None:
            assert citation["table"] == source["table"]
            for key in ("record_id_column", "evidence_id_column", "row_id_column"):
                assert citation[key] in cols
        for col in cols.values():
            if "param_type" in col:
                assert col["param_type"] in layer["param_types"]


def test_joins_and_metrics_reference_real_columns(layer: dict[str, Any]) -> None:
    sources = layer["sources"]
    for join in layer["joins"].values():
        for side in ("from", "to"):
            ref = join[side]
            assert ref["column"] in sources[ref["source"]]["columns"]
        for pred in join.get("to_filters", []):
            assert pred["column"] in sources[join["to"]["source"]]["columns"]
    for metric in layer["metrics"].values():
        assert metric["source"] in sources
        if "column" in metric:
            assert metric["column"] in sources[metric["source"]]["columns"]


@pytest.mark.parametrize(
    "op_name",
    sorted(load_semantic_layer()["operations"]),
)
def test_operation_is_consistent(layer: dict[str, Any], op_name: str) -> None:
    op = layer["operations"][op_name]
    source = layer["sources"][op["source"]]
    columns = source["columns"]
    assert source["citation"] is not None, "operations must return citable rows"
    used_params: set[str] = set()
    for pred in _predicates(op["filters"]):
        assert pred["column"] in columns
        assert pred["column"] != source["case_column"], "case_id is injected by the compiler"
        for param in _filter_params(pred):
            assert param in op["params"], f"{op_name}: undeclared param {param}"
            used_params.add(param)
    assert used_params == set(op["params"]), f"{op_name}: declared params unused"
    for param in op["params"].values():
        assert param["type"] in layer["param_types"]
    for col in op.get("select", []):
        assert col in columns
    for order in op.get("order_by", []):
        assert order["column"] in columns
    if "metric" in op:
        assert layer["metrics"][op["metric"]]["source"] == op["source"]


def test_no_raw_sql_fragments(layer: dict[str, Any]) -> None:
    for op in layer["operations"].values():
        for pred in _predicates(op["filters"]):
            assert set(pred) <= {"column", "op", "param", "params"}


def test_operation_names_match_query_records_tool(layer: dict[str, Any]) -> None:
    tool = json.loads(QUERY_RECORDS_TOOL.read_text(encoding="utf-8"))
    enum = tool["input_schema"]["properties"]["operation"]["enum"]
    assert enum == list(layer["operations"])
    branches = {
        b["if"]["properties"]["operation"]["const"]: b["then"]["properties"]["params"]
        for b in tool["input_schema"]["allOf"]
    }
    for name, op in layer["operations"].items():
        assert set(branches[name]["properties"]) == set(op["params"])
        required = {k for k, p in op["params"].items() if p["required"]}
        assert set(branches[name]["required"]) == required
