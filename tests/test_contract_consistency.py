"""Cross-contract checks: the OpenAPI spec, agent schemas, and semantic layer must agree.

All sample data is SYNTHETIC.
"""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from caselens_agent.contracts import load_answer_schema, load_tool_definitions
from caselens_semantic_layer.contracts import load_semantic_layer

SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "contracts" / "openapi.yaml"
SPEC_URI = "urn:caselens:openapi-consistency"

SAMPLE_ANSWER: dict[str, Any] = {
    "status": "answered",
    "answer": "SYNTHETIC: the gray sedan (plate SYN4821) entered the Riverside lot at 22:41.",
    "claims": [
        {"id": "cl1", "text": "A plate reader logged SYN4821 at 22:41.", "citation_ids": ["c1"]},
        {"id": "cl2", "text": "CCTV shows a gray sedan entering the lot.", "citation_ids": ["c2"]},
    ],
    "citations": [
        {
            "id": "c1",
            "source_type": "record",
            "evidence_id": "00000000-0000-4000-8000-000000000001",
            "table": "plate_reads",
            "record_id": "00000000-0000-4000-8000-000000000002",
            "locator": {"kind": "record", "table": "plate_reads", "row_id": 17},
        },
        {
            "id": "c2",
            "source_type": "chunk",
            "evidence_id": "00000000-0000-4000-8000-000000000003",
            "chunk_id": "00000000-0000-4000-8000-000000000004",
            "locator": {"kind": "video", "start_ms": 65000, "end_ms": 70000, "frame_idx": 1950},
        },
    ],
    "conflicts": [],
    "confidence": 0.82,
    "open_questions": [],
}


def _openapi_answer_validator(spec: dict[str, Any]) -> Draft202012Validator:
    registry: Registry[Any] = Registry().with_resource(
        SPEC_URI, Resource.from_contents(spec, default_specification=DRAFT202012)
    )
    return Draft202012Validator(
        {"$ref": f"{SPEC_URI}#/components/schemas/AnswerContract"}, registry=registry
    )


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    return loaded


def _mutations() -> list[tuple[str, dict[str, Any]]]:
    no_claims = copy.deepcopy(SAMPLE_ANSWER)
    no_claims["claims"] = []
    uncited = copy.deepcopy(SAMPLE_ANSWER)
    uncited["claims"][0]["citation_ids"] = []
    bad_kind = copy.deepcopy(SAMPLE_ANSWER)
    bad_kind["citations"][1]["locator"]["kind"] = "hologram"
    extra = copy.deepcopy(SAMPLE_ANSWER)
    extra["verdict"] = "guilty"
    refused = copy.deepcopy(SAMPLE_ANSWER)
    refused.update(status="insufficient_evidence", claims=[], citations=[], confidence=0.0)
    return [
        ("valid", SAMPLE_ANSWER),
        ("refused", refused),
        ("no_claims", no_claims),
        ("uncited", uncited),
        ("bad_kind", bad_kind),
        ("extra", extra),
    ]


@pytest.mark.parametrize(("label", "answer"), _mutations())
def test_openapi_and_agent_answer_schemas_agree(
    spec: dict[str, Any], label: str, answer: dict[str, Any]
) -> None:
    agent_ok = Draft202012Validator(load_answer_schema()).is_valid(answer)
    api_ok = _openapi_answer_validator(spec).is_valid(answer)
    assert agent_ok == api_ok, label
    assert agent_ok == (label in {"valid", "refused"}), label


def test_openapi_tool_names_match_tool_files(spec: dict[str, Any]) -> None:
    assert set(spec["components"]["schemas"]["ToolName"]["enum"]) == set(load_tool_definitions())


def test_query_records_operations_match_semantic_layer() -> None:
    tool = load_tool_definitions()["query_records"]
    enum = tool["input_schema"]["properties"]["operation"]["enum"]
    assert set(enum) == set(load_semantic_layer()["operations"])


def test_tools_never_accept_case_id() -> None:
    for name, tool in load_tool_definitions().items():
        assert "case_id" not in tool["input_schema"].get("properties", {}), name
