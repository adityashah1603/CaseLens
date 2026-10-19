"""Contract tests for docs/contracts/openapi.yaml.

All sample data in this file is SYNTHETIC (fictional case, people, plates and IDs).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from openapi_spec_validator import OpenAPIV31SpecValidator, validate
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "contracts" / "openapi.yaml"
SPEC_URI = "urn:caselens:openapi"
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}

# Canonical enums from the shared contract brief.
EXPECTED_ENUMS: dict[str, list[str]] = {
    "Role": ["lead", "analyst", "viewer"],
    "EvidenceType": ["pdf", "audio", "video", "csv"],
    "IngestJobStatus": ["queued", "running", "succeeded", "failed", "dead_lettered"],
    "EntityType": ["person", "vehicle", "phone", "location", "weapon"],
    "AnswerStatus": ["answered", "insufficient_evidence"],
    "CaseStatus": ["open", "closed", "archived"],
    "EntityLinkMethod": ["exact", "fuzzy", "llm", "manual"],
    "EntityLinkStatus": ["active", "reverted"],
    "ConflictStatus": ["open", "reviewed"],
    "AgentRunStatus": ["running", "succeeded", "failed"],
}

# SYNTHETIC answer: fictional case, fictional IDs.
SYNTHETIC_ANSWER: dict[str, Any] = {
    "status": "answered",
    "answer": "SYNTHETIC: Two people were seen in the gray sedan at the Riverside lot on March 14.",
    "claims": [
        {
            "id": "cl1",
            "text": "A witness saw two people in a gray sedan at the Riverside lot.",
            "citation_ids": ["c1"],
            "t_start": "2026-03-14T22:15:00Z",
            "t_end": None,
        },
        {
            "id": "cl2",
            "text": "CCTV shows the gray sedan entering the lot at 10:41pm.",
            "citation_ids": ["c2", "c3"],
            "t_start": "2026-03-14T22:41:00Z",
        },
    ],
    "citations": [
        {
            "id": "c1",
            "source_type": "chunk",
            "evidence_id": "00000000-0000-4000-8000-000000000e01",
            "chunk_id": "00000000-0000-4000-8000-000000000c01",
            "table": None,
            "record_id": None,
            "locator": {
                "kind": "pdf",
                "page": 2,
                "bbox": [72.0, 340.5, 520.0, 388.0],
                "ocr_confidence": None,
            },
            "quote": "I saw two people sitting in a gray sedan.",
        },
        {
            "id": "c2",
            "source_type": "chunk",
            "evidence_id": "00000000-0000-4000-8000-000000000e02",
            "chunk_id": "00000000-0000-4000-8000-000000000c02",
            "table": None,
            "record_id": None,
            "locator": {
                "kind": "video",
                "start_ms": 61000,
                "end_ms": 68500,
                "frame_idx": 1830,
                "bbox": [410, 220, 690, 400],
                "track_id": "t7",
            },
            "quote": None,
        },
        {
            "id": "c3",
            "source_type": "record",
            "evidence_id": "00000000-0000-4000-8000-000000000e03",
            "chunk_id": None,
            "table": "plate_reads",
            "record_id": "00000000-0000-4000-8000-000000000d01",
            "locator": {"kind": "record", "table": "plate_reads", "row_id": 118},
            "quote": None,
        },
    ],
    "conflicts": [
        {
            "id": "cf1",
            "description": "Witness time and camera time disagree.",
            "claim_a": {"text": "The sedan arrived at 10:15pm.", "citation_ids": ["c1"]},
            "claim_b": {"text": "The sedan arrived at 10:41pm.", "citation_ids": ["c2"]},
            "conflict_record_id": None,
        }
    ],
    "confidence": 0.82,
    "open_questions": ["Who was driving the gray sedan?"],
}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    with SPEC_PATH.open(encoding="utf-8") as f:
        loaded: dict[str, Any] = yaml.safe_load(f)
    return loaded


@pytest.fixture(scope="module")
def answer_validator(spec: dict[str, Any]) -> Draft202012Validator:
    resource: Resource[Any] = Resource.from_contents(spec, default_specification=DRAFT202012)
    registry: Registry[Any] = Registry().with_resource(SPEC_URI, resource)
    schema = {"$ref": f"{SPEC_URI}#/components/schemas/AnswerContract"}
    return Draft202012Validator(schema, registry=registry)


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    ops: list[tuple[str, str, dict[str, Any]]] = []
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in HTTP_METHODS:
                ops.append((path, method, op))
    return ops


def _resolve(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if ref is None:
        return node
    target: Any = spec
    for part in str(ref).removeprefix("#/").split("/"):
        target = target[part]
    resolved: dict[str, Any] = target
    return resolved


def test_spec_is_valid_openapi_31(spec: dict[str, Any]) -> None:
    assert spec["openapi"] == "3.1.0"
    validate(spec, cls=OpenAPIV31SpecValidator)


def test_operation_ids_are_present_and_unique(spec: dict[str, Any]) -> None:
    ids = [op.get("operationId") for _, _, op in _operations(spec)]
    assert all(ids), "every operation needs an operationId"
    assert len(ids) == len(set(ids))


def test_paths_are_versioned(spec: dict[str, Any]) -> None:
    for path in spec["paths"]:
        assert path == "/healthz" or path.startswith("/v1/"), path


def test_bearer_auth_everywhere_except_healthz(spec: dict[str, Any]) -> None:
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    global_security = spec["security"]
    for path, method, op in _operations(spec):
        effective = op.get("security", global_security)
        if path == "/healthz":
            assert effective == [], "healthz must be unauthenticated"
        else:
            assert {"bearerAuth": []} in effective, f"{method.upper()} {path}"


def test_every_4xx_response_uses_problem(spec: dict[str, Any]) -> None:
    for path, method, op in _operations(spec):
        for code, response in op["responses"].items():
            if not str(code).startswith("4"):
                continue
            resolved = _resolve(spec, response)
            content = resolved.get("content", {})
            assert list(content) == ["application/problem+json"], f"{method} {path} {code}"
            schema = content["application/problem+json"]["schema"]
            assert schema == {"$ref": "#/components/schemas/Problem"}, f"{method} {path} {code}"


@pytest.mark.parametrize(("name", "expected"), sorted(EXPECTED_ENUMS.items()))
def test_enums_match_brief(spec: dict[str, Any], name: str, expected: list[str]) -> None:
    assert spec["components"]["schemas"][name]["enum"] == expected


def test_answer_contract_properties(spec: dict[str, Any]) -> None:
    schema = spec["components"]["schemas"]["AnswerContract"]
    expected = {
        "status",
        "answer",
        "claims",
        "citations",
        "conflicts",
        "confidence",
        "open_questions",
    }
    assert set(schema["properties"]) == expected
    assert set(schema["required"]) == expected
    assert schema["additionalProperties"] is False


def test_synthetic_answer_validates(answer_validator: Draft202012Validator) -> None:
    answer_validator.validate(SYNTHETIC_ANSWER)


def test_insufficient_evidence_answer_validates(answer_validator: Draft202012Validator) -> None:
    refusal: dict[str, Any] = {
        "status": "insufficient_evidence",
        "answer": "SYNTHETIC: The case file does not say who owned the sedan.",
        "claims": [],
        "citations": [],
        "conflicts": [],
        "confidence": 0.0,
        "open_questions": ["Is there a vehicle registration record?"],
    }
    answer_validator.validate(refusal)


def test_answered_without_claims_is_rejected(answer_validator: Draft202012Validator) -> None:
    bad = copy.deepcopy(SYNTHETIC_ANSWER)
    bad["claims"] = []
    assert not answer_validator.is_valid(bad)


def test_claim_without_citation_is_rejected(answer_validator: Draft202012Validator) -> None:
    bad = copy.deepcopy(SYNTHETIC_ANSWER)
    bad["claims"][0]["citation_ids"] = []
    assert not answer_validator.is_valid(bad)


def test_unknown_locator_kind_is_rejected(answer_validator: Draft202012Validator) -> None:
    bad = copy.deepcopy(SYNTHETIC_ANSWER)
    bad["citations"][0]["locator"] = {"kind": "hologram", "page": 1}
    assert not answer_validator.is_valid(bad)


def test_extra_answer_field_is_rejected(answer_validator: Draft202012Validator) -> None:
    bad = copy.deepcopy(SYNTHETIC_ANSWER)
    bad["reasoning"] = "not part of the contract"
    assert not answer_validator.is_valid(bad)


def test_request_bodies_forbid_extra_properties(spec: dict[str, Any]) -> None:
    for path, method, op in _operations(spec):
        body = op.get("requestBody")
        if body is None:
            continue
        for media in body["content"].values():
            schema = _resolve(spec, media["schema"])
            assert schema.get("additionalProperties") is False, f"{method} {path}"


def test_evidence_has_recorded_at_and_ingest_jobs(spec: dict[str, Any]) -> None:
    schemas = spec["components"]["schemas"]
    evidence = schemas["Evidence"]
    assert evidence["properties"]["recorded_at"]["type"] == ["string", "null"]
    assert {"recorded_at", "ingest_jobs"} <= set(evidence["required"])
    upload = schemas["EvidenceUpload"]
    assert "recorded_at" in upload["properties"]
    assert upload["required"] == ["file"]


def test_case_has_demo_fields(spec: dict[str, Any]) -> None:
    props = spec["components"]["schemas"]["Case"]["properties"]
    assert {"suggested_questions", "evidence_count", "last_activity_at"} <= set(props)
