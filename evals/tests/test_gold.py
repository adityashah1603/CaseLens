"""Gold set tests (SYNTHETIC data). No database or LLM: structured results come from the cache."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import PurePosixPath
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from caselens_agent.contracts import load_tool_definitions
from caselens_evals import build_gold
from caselens_evals.gold import (
    MIX,
    GoldError,
    GoldItem,
    RecordLocator,
    RequiredCitation,
    check_mix,
    load_gold,
    match_citation,
)

if not (build_gold.ANSWER_KEY_PATH.exists() and build_gold.MANIFEST_PATH.exists()):
    # The answer key and manifest are generated data_gen output (gitignored); CI has neither.
    pytest.skip(
        "data_gen output not present (run python -m caselens_data_gen)", allow_module_level=True
    )

ITEMS = load_gold(build_gold.GOLD_PATH)
FACTS: list[dict[str, Any]] = json.loads(build_gold.ANSWER_KEY_PATH.read_text(encoding="utf-8"))[
    "facts"
]
MANIFEST = {
    f["path"] for f in json.loads(build_gold.MANIFEST_PATH.read_text(encoding="utf-8"))["files"]
}


def test_committed_file_loads_with_exact_mix() -> None:
    assert len(ITEMS) == 50
    assert [i.id for i in ITEMS] == [f"q{n:02d}" for n in range(1, 51)]
    assert dict(Counter(i.category for i in ITEMS)) == MIX
    check_mix(ITEMS)


def test_every_required_citation_is_an_answer_key_fact_in_the_manifest() -> None:
    key = {(f["fact_id"], f["evidence"], json.dumps(f["locator"], sort_keys=True)) for f in FACTS}
    for item in ITEMS:
        for c in item.required_citations:
            assert (c.fact_id, c.evidence, json.dumps(c.locator_dict(), sort_keys=True)) in key
            assert c.evidence in MANIFEST


def test_source_ids_are_story_ids() -> None:
    truth = build_gold.load_truth()
    for item in ITEMS:
        assert set(item.source_ids) <= truth.story_ids, item.id


def test_structured_items_pass_the_query_records_input_schema() -> None:
    schema = load_tool_definitions()["query_records"]["input_schema"]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    ops = set()
    for item in ITEMS:
        if item.sql is None:
            continue
        validator.validate({"operation": item.sql.operation, "params": item.sql.params})
        ops.add(item.sql.operation)
        cited = [
            c.locator.row_id
            for c in item.required_citations
            if isinstance(c.locator, RecordLocator)
        ]
        assert len(cited) == len(item.required_citations)
        assert cited and item.sql.expected.row_count is not None
        if item.sql.operation != "call_count_between":
            assert item.sql.expected.row_count == len(cited)
    assert ops == {
        "calls_between",
        "call_count_between",
        "calls_for_phone",
        "plate_sightings",
        "plate_sightings_near",
        "custody_chain",
    }


def test_known_facts() -> None:
    by_id = {i.id: i for i in ITEMS}
    count = by_id["q31"].sql
    assert count is not None and count.expected.rows_match == [{"call_count": 7}]
    conflicts = sorted(i.conflict.id for i in ITEMS if i.conflict is not None)
    assert conflicts == ["cf_01", "cf_01", "cf_02", "cf_03", "cf_04"]
    for item in ITEMS:  # the vid_02 22:41:21 exit artifact is never a gold conflict (D-043)
        assert "22:41:21" not in json.dumps(item.to_json_dict())
    assert by_id["q16"].question == "Who was in the gray sedan on March 14?"


def test_rebuild_is_byte_identical() -> None:
    truth = build_gold.load_truth()
    text = build_gold.render(build_gold.build_items(truth, build_gold.cached_sql()))
    assert text.encode("utf-8") == build_gold.GOLD_PATH.read_bytes()
    assert build_gold.main(["--check"]) == 0


def test_rebuild_without_cached_sql_fails() -> None:
    with pytest.raises(GoldError, match="refresh-sql"):
        build_gold.build_items(build_gold.load_truth(), {})


def _item(**over: Any) -> dict[str, Any]:
    base = ITEMS[0].to_json_dict()
    base.update(over)
    return base


def test_item_validators() -> None:
    cite = ITEMS[0].to_json_dict()["required_citations"]
    bad = [
        _item(required_citations=[]),
        _item(expected_status="insufficient_evidence"),
        _item(conflict={"id": "cf_01", "sides": ["a", "b"]}),
        _item(modalities=["pdf", "csv"]),
        _item(id="q1"),
        _item(extra_field=1),
        _item(required_citations=cite + cite),
        _item(category="unanswerable", expected_status="insufficient_evidence"),
    ]
    for data in bad:
        with pytest.raises(ValueError):
            GoldItem.model_validate_json(json.dumps(data))
    with pytest.raises(GoldError):
        check_mix(ITEMS[:49])


def _req(evidence: str, locator: dict[str, Any]) -> RequiredCitation:
    return RequiredCitation.model_validate(
        {"fact_id": "x", "evidence": evidence, "locator": locator}, strict=False
    )


def test_match_citation_rule() -> None:
    pdf = _req("pdf/ws_ruiz.pdf", {"kind": "pdf", "page": 1, "bbox": [10, 10, 20, 20]})
    assert match_citation("ws_ruiz.pdf", {"kind": "pdf", "page": 1, "bbox": [15, 15, 30, 30]}, pdf)
    assert match_citation("ws_ruiz.pdf", {"kind": "pdf", "page": 1, "bbox": None}, pdf)
    assert not match_citation(
        "ws_ruiz.pdf", {"kind": "pdf", "page": 1, "bbox": [21, 0, 30, 9]}, pdf
    )
    assert not match_citation("ws_ruiz.pdf", {"kind": "pdf", "page": 2}, pdf)
    assert not match_citation("ws_pike.pdf", {"kind": "pdf", "page": 1}, pdf)

    audio = _req("audio/aud_911.wav", {"kind": "audio", "start_ms": 1000, "end_ms": 2000})
    assert match_citation("aud_911.wav", {"kind": "audio", "start_ms": 1900, "end_ms": 5000}, audio)
    assert not match_citation(
        "aud_911.wav", {"kind": "audio", "start_ms": 2001, "end_ms": 3000}, audio
    )

    video = _req("video/v.mp4", {"kind": "video", "start_ms": 10000, "end_ms": 20000})
    assert match_citation("v.mp4", {"kind": "video", "start_ms": 19000, "end_ms": 25000}, video)
    assert match_citation("v.mp4", {"kind": "video", "start_ms": 21500, "end_ms": None}, video)
    assert not match_citation("v.mp4", {"kind": "video", "start_ms": 22500}, video)
    point = _req("video/v.mp4", {"kind": "video", "start_ms": 10000})
    assert match_citation("v.mp4", {"kind": "video", "start_ms": 11900}, point)
    assert not match_citation("v.mp4", {"kind": "video", "start_ms": 12100}, point)

    rec = _req("csv/c.csv", {"kind": "record", "table": "call_records", "row_id": 7})
    assert match_citation("c.csv", {"kind": "record", "table": "call_records", "row_id": 7}, rec)
    assert not match_citation("c.csv", {"kind": "record", "table": "plate_reads", "row_id": 7}, rec)
    assert not match_citation("c.csv", {"kind": "pdf", "page": 7}, rec)
    assert PurePosixPath(rec.evidence).name == rec.filename
