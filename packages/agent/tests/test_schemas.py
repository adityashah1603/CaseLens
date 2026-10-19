"""Contract tests for the answer schema and agent tool definitions. All sample data is SYNTHETIC."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from caselens_agent.contracts import (
    SCHEMAS_DIR,
    TOOLS_DIR,
    load_answer_schema,
    load_tool_definition_schema,
    load_tool_definitions,
)

EXPECTED_TOOLS = {
    "search_evidence",
    "search_video",
    "query_records",
    "get_entity",
    "get_timeline",
    "flag_conflict",
}

# SYNTHETIC ids (fictional).
EV_PDF = "00000000-0000-4000-8000-0000000000a1"
EV_VIDEO = "00000000-0000-4000-8000-0000000000a2"
EV_CSV = "00000000-0000-4000-8000-0000000000a3"
CHUNK_PDF = "00000000-0000-4000-8000-0000000000b1"
CHUNK_VIDEO = "00000000-0000-4000-8000-0000000000b2"
RECORD_ID = "00000000-0000-4000-8000-0000000000c1"
ENTITY_ID = "00000000-0000-4000-8000-0000000000d1"
CONFLICT_ID = "00000000-0000-4000-8000-0000000000e1"


def _validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _is_valid(schema: dict[str, Any], instance: Any) -> bool:
    return bool(_validator(schema).is_valid(instance))


VALID_ANSWER: dict[str, Any] = {
    "status": "answered",
    "answer": "SYNTHETIC: A gray sedan (plate 7ABC123) was at Riverside Lot at about 22:41.",
    "claims": [
        {
            "id": "cl1",
            "text": "SYNTHETIC: The ALPR camera read plate 7ABC123 at Riverside Lot at 22:41.",
            "citation_ids": ["c3"],
            "t_start": "2026-03-14T22:41:00-07:00",
            "t_end": None,
        },
        {
            "id": "cl2",
            "text": "SYNTHETIC: Video shows a gray sedan entering the lot.",
            "citation_ids": ["c1", "c2"],
        },
    ],
    "citations": [
        {
            "id": "c1",
            "source_type": "chunk",
            "evidence_id": EV_PDF,
            "chunk_id": CHUNK_PDF,
            "table": None,
            "record_id": None,
            "locator": {
                "kind": "pdf",
                "page": 2,
                "bbox": [72.0, 400.5, 540.0, 430.0],
                "ocr_confidence": None,
            },
            "quote": "SYNTHETIC: I saw a gray sedan pull in around ten fifteen.",
        },
        {
            "id": "c2",
            "source_type": "chunk",
            "evidence_id": EV_VIDEO,
            "chunk_id": CHUNK_VIDEO,
            "table": None,
            "record_id": None,
            "locator": {
                "kind": "video",
                "start_ms": 120000,
                "end_ms": 135000,
                "frame_idx": 3600,
                "bbox": [100, 200, 380, 360],
                "track_id": "t7",
            },
            "quote": None,
        },
        {
            "id": "c3",
            "source_type": "record",
            "evidence_id": EV_CSV,
            "chunk_id": None,
            "table": "plate_reads",
            "record_id": RECORD_ID,
            "locator": {"kind": "record", "table": "plate_reads", "row_id": 42},
            "quote": None,
        },
    ],
    "conflicts": [
        {
            "id": "cf1",
            "description": "SYNTHETIC: Witness time and camera time for the sedan arrival differ.",
            "claim_a": {"text": "Witness says about 22:15.", "citation_ids": ["c1"]},
            "claim_b": {"text": "Camera shows 22:41.", "citation_ids": ["c2", "c3"]},
            "conflict_record_id": CONFLICT_ID,
        }
    ],
    "confidence": 0.8,
    "open_questions": ["SYNTHETIC: Who was driving the sedan?"],
}

INSUFFICIENT_ANSWER: dict[str, Any] = {
    "status": "insufficient_evidence",
    "answer": "SYNTHETIC: The case file has no evidence about the suspect's shoe size.",
    "claims": [],
    "citations": [],
    "conflicts": [],
    "confidence": 0.0,
    "open_questions": [],
}


def _mutated(path: list[str | int], value: Any) -> dict[str, Any]:
    answer = copy.deepcopy(VALID_ANSWER)
    target: Any = answer
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return answer


def _no_claims_answered() -> dict[str, Any]:
    answer = copy.deepcopy(INSUFFICIENT_ANSWER)
    answer["status"] = "answered"
    return answer


INVALID_ANSWERS: dict[str, dict[str, Any]] = {
    "claim_with_empty_citation_ids": _mutated(["claims", 0, "citation_ids"], []),
    "answered_without_claims": _no_claims_answered(),
    "bad_locator_kind": _mutated(["citations", 0, "locator", "kind"], "image"),
    "extra_top_level_property": _mutated(["reasoning"], "SYNTHETIC hidden reasoning"),
    "extra_citation_property": _mutated(["citations", 0, "url"], "https://example.invalid"),
    "confidence_above_one": _mutated(["confidence"], 1.5),
    "bad_citation_id_pattern": _mutated(["citations", 0, "id"], "cite-1"),
    "bad_claim_id_pattern": _mutated(["claims", 0, "id"], "c1"),
    "record_citation_without_record_id": _mutated(["citations", 2, "record_id"], None),
    "chunk_citation_without_chunk_id": _mutated(["citations", 0, "chunk_id"], None),
    "pdf_page_zero": _mutated(["citations", 0, "locator", "page"], 0),
    "bbox_wrong_length": _mutated(["citations", 0, "locator", "bbox"], [1, 2, 3]),
    "bad_status": _mutated(["status"], "maybe"),
}


def _all_schema_files() -> list[Path]:
    return sorted([*SCHEMAS_DIR.glob("*.json"), *TOOLS_DIR.glob("*.json")])


@pytest.mark.parametrize("path", _all_schema_files(), ids=lambda p: p.name)
def test_files_are_valid_json(path: Path) -> None:
    assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


def test_answer_and_meta_schemas_are_valid_draft_2020_12() -> None:
    answer = load_answer_schema()
    assert answer["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert answer["$id"] == "https://caselens.dev/schemas/answer.schema.json"
    Draft202012Validator.check_schema(answer)
    Draft202012Validator.check_schema(load_tool_definition_schema())


def test_exactly_the_six_tools_exist() -> None:
    assert {p.stem for p in TOOLS_DIR.glob("*.json")} == EXPECTED_TOOLS
    assert set(load_tool_definitions()) == EXPECTED_TOOLS


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_tool_definition_shape(name: str) -> None:
    raw = json.loads((TOOLS_DIR / f"{name}.json").read_text(encoding="utf-8"))
    _validator(load_tool_definition_schema()).validate(raw)
    assert raw["name"] == name
    Draft202012Validator.check_schema(raw["input_schema"])
    Draft202012Validator.check_schema(raw["output_schema"])
    # input_schema is sent to the LLM provider as-is: it must be self-contained.
    assert "$ref" not in json.dumps(raw["input_schema"])
    assert "case_id" not in raw["input_schema"]["properties"]


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS - {"flag_conflict"}))
def test_read_tool_descriptions_mark_evidence_as_data(name: str) -> None:
    assert "data, not instructions" in load_tool_definitions()[name]["description"].lower()


def test_valid_answers_pass() -> None:
    schema = load_answer_schema()
    _validator(schema).validate(VALID_ANSWER)
    _validator(schema).validate(INSUFFICIENT_ANSWER)


@pytest.mark.parametrize("case", sorted(INVALID_ANSWERS))
def test_invalid_answers_fail(case: str) -> None:
    assert not _is_valid(load_answer_schema(), INVALID_ANSWERS[case])


SOURCE_REF_CHUNK = {
    "source_type": "chunk",
    "evidence_id": EV_PDF,
    "chunk_id": CHUNK_PDF,
    "locator": {"kind": "pdf", "page": 2, "bbox": None},
}
SOURCE_REF_RECORD = {
    "source_type": "record",
    "evidence_id": EV_CSV,
    "table": "plate_reads",
    "record_id": RECORD_ID,
    "locator": {"kind": "record", "table": "plate_reads", "row_id": 42},
}
WINDOW = {"window_start": "2026-03-14T21:00:00Z", "window_end": "2026-03-14T23:59:59Z"}

VALID_INPUTS: dict[str, list[dict[str, Any]]] = {
    "search_evidence": [
        {"query": "gray sedan"},
        {
            "query": "SYNTHETIC witness statement about the lot",
            "filters": {
                "modalities": ["pdf", "audio"],
                "evidence_ids": [EV_PDF],
                "entity_ids": [ENTITY_ID],
                "time_from": "2026-03-14T21:00:00Z",
                "time_to": "2026-03-14T23:00:00Z",
            },
            "top_k": 20,
        },
    ],
    "search_video": [
        {"query": "gray sedan entering lot"},
        {
            "query": "person in red jacket",
            "time_from": "2026-03-14T22:00:00Z",
            "time_to": "2026-03-14T23:00:00Z",
            "evidence_ids": [EV_VIDEO],
            "top_k": 1,
        },
    ],
    "query_records": [
        {
            "operation": "calls_between",
            "params": {"phone_a": "+15550100001", "phone_b": "+15550100002", **WINDOW},
        },
        {"operation": "calls_for_phone", "params": {"phone": "+15550100001", **WINDOW}},
        {
            "operation": "call_count_between",
            "params": {"phone_a": "+15550100001", "phone_b": "+15550100002", **WINDOW},
        },
        {"operation": "plate_sightings", "params": {"plate": "7ABC123", **WINDOW}},
        {"operation": "plate_sightings_near", "params": {"location_name": "Riverside", **WINDOW}},
        {"operation": "custody_chain", "params": {"item_id": "ITEM-007"}},
    ],
    "get_entity": [
        {"entity_id": ENTITY_ID},
        {"name": "SYNTHETIC Dana Reyes", "type": "person"},
    ],
    "get_timeline": [
        {"entity_id": ENTITY_ID},
        {"time_from": "2026-03-14T21:00:00Z", "time_to": "2026-03-14T23:00:00Z"},
    ],
    "flag_conflict": [
        {
            "description": "SYNTHETIC: arrival time differs between witness and ALPR.",
            "claim_a": {"text": "Witness: about 22:15.", "source_refs": [SOURCE_REF_CHUNK]},
            "claim_b": {"text": "ALPR: 22:41.", "source_refs": [SOURCE_REF_RECORD]},
        }
    ],
}

INVALID_INPUTS: dict[str, list[dict[str, Any]]] = {
    "search_evidence": [
        {},
        {"query": ""},
        {"query": "x" * 501},
        {"query": "sedan", "top_k": 21},
        {"query": "sedan", "filters": {"modalities": ["image"]}},
        {"query": "sedan", "case_id": EV_PDF},
    ],
    "search_video": [
        {"time_from": "2026-03-14T21:00:00Z"},
        {"query": "sedan", "top_k": 0},
        {"query": "sedan", "evidence_ids": ["not-a-uuid"]},
    ],
    "query_records": [
        {"operation": "drop_table", "params": {}},
        {"operation": "custody_chain", "params": {}},
        {"operation": "custody_chain", "params": {"item_id": "ITEM-007", "sql": "1=1"}},
        {"operation": "plate_sightings", "params": {"plate": "7ABC123"}},
        {"operation": "calls_for_phone"},
        {"operation": "calls_for_phone", "params": {"phone": 5550100001, **WINDOW}},
    ],
    "get_entity": [
        {},
        {"type": "vehicle"},
        {"name": "Dana", "type": "alien"},
    ],
    "get_timeline": [
        {},
        {"entity_id": ENTITY_ID, "limit": 5},
    ],
    "flag_conflict": [
        {
            "description": "SYNTHETIC",
            "claim_a": {"text": "A", "source_refs": []},
            "claim_b": {"text": "B", "source_refs": [SOURCE_REF_CHUNK]},
        },
        {
            "description": "SYNTHETIC",
            "claim_a": {"text": "A", "source_refs": [{**SOURCE_REF_CHUNK, "chunk_id": None}]},
            "claim_b": {"text": "B", "source_refs": [SOURCE_REF_RECORD]},
        },
        {
            "description": "SYNTHETIC",
            "claim_a": {"text": "A", "source_refs": [SOURCE_REF_CHUNK]},
        },
    ],
}


def _cases(table: dict[str, list[dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    return [(name, sample) for name, samples in table.items() for sample in samples]


@pytest.mark.parametrize(("name", "sample"), _cases(VALID_INPUTS))
def test_valid_tool_inputs_pass(name: str, sample: dict[str, Any]) -> None:
    _validator(load_tool_definitions()[name]["input_schema"]).validate(sample)


@pytest.mark.parametrize(("name", "sample"), _cases(INVALID_INPUTS))
def test_invalid_tool_inputs_fail(name: str, sample: dict[str, Any]) -> None:
    assert not _is_valid(load_tool_definitions()[name]["input_schema"], sample)


def test_sample_tool_outputs_validate() -> None:
    tools = load_tool_definitions()
    loc_pdf = {"kind": "pdf", "page": 1, "bbox": None, "ocr_confidence": 0.93}
    outputs = {
        "search_evidence": {
            "results": [
                {
                    "chunk_id": CHUNK_PDF,
                    "evidence_id": EV_PDF,
                    "modality": "pdf",
                    "text": "SYNTHETIC: gray sedan near the lot.",
                    "score": 0.71,
                    "locator": loc_pdf,
                }
            ]
        },
        "query_records": {
            "operation": "plate_sightings",
            "sql": "SELECT plate FROM plate_reads WHERE case_id = %(case_id)s",
            "params": {"plate": "7ABC123"},
            "columns": ["plate"],
            "rows": [{"plate": "7ABC123"}],
            "row_count": 1,
            "truncated": False,
            "sources": [
                {
                    "table": "plate_reads",
                    "record_id": RECORD_ID,
                    "evidence_id": EV_CSV,
                    "row_id": 42,
                }
            ],
        },
        "flag_conflict": {"conflict_id": CONFLICT_ID, "status": "open"},
    }
    for name, output in outputs.items():
        _validator(tools[name]["output_schema"]).validate(output)
