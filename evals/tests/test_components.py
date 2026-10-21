"""Component-eval math on tiny SYNTHETIC inputs, plus a gated run against the dev case.

The heavy test reads the ingested dev DB + MinIO (read-only) and only runs with CASELENS_HEAVY=1.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from caselens_evals.components import metrics as m
from caselens_evals.components.asr import overlap_pairs, strip_speaker
from caselens_evals.components.ocr import hypothesis_pages, strip_overlap
from caselens_evals.components.sources import Chunk
from caselens_evals.components.video import track_box_at

# --- CER / WER ---------------------------------------------------------------------------


def test_cer_counts_edits_over_reference_length() -> None:
    assert m.cer("abcd", "abcd") == 0.0
    assert m.cer("abcd", "abXd") == 0.25  # one substitution
    assert m.cer("abcd", "abd") == 0.25  # one deletion
    assert m.cer("ab", "abcd") == 1.0  # two insertions over two reference chars
    assert m.cer("", "") == 0.0
    assert m.cer("", "x") == 1.0


def test_cer_normalizations() -> None:
    gold, hyp = "Gerald  Pike\nAlder Row", "gerald pike alder  row"
    assert m.cer(m.collapse_ws(gold), m.collapse_ws(hyp)) > 0
    assert m.cer(m.normalize_ocr(gold), m.normalize_ocr(hyp)) == 0.0
    assert m.strip_ws("Iwalk my beagle") == m.strip_ws("I walk mybeagle")


def test_wer_word_level() -> None:
    ref = ["the", "gray", "sedan", "left"]
    assert m.wer(ref, ref) == 0.0
    assert m.wer(ref, ["the", "grey", "sedan", "left"]) == 0.25
    assert m.wer(ref, ["the", "gray", "sedan"]) == 0.25
    assert m.wer(ref, ["the", "big", "gray", "sedan", "left"]) == 0.25


@pytest.mark.parametrize(
    ("digits", "words"),
    [
        ("7", "seven"),
        ("42", "forty two"),
        ("911", "nine eleven"),
        ("200", "two hundred"),
        ("1214", "twelve fourteen"),
        ("1200", "twelve hundred"),
        ("2026", "twenty twenty six"),
        ("2005", "twenty oh five"),
        ("0871", "zero eight seven one"),
        ("5550142", "five five five zero one four two"),
    ],
)
def test_number_words(digits: str, words: str) -> None:
    assert m.number_words(digits) == words.split()


def test_normalize_words_spells_numbers_and_strips_punctuation() -> None:
    assert m.normalize_words("911, what's the address?") == [
        "nine",
        "eleven",
        "whats",
        "the",
        "address",
    ]
    assert m.normalize_words("March 16th, 2:10 PM") == ["march", "sixteenth", "two", "ten", "pm"]
    assert m.raw_words("Ma'am, 1214 Calder!") == ["maam", "1214", "calder"]
    assert m.normalize_words("twelve hundred block") == m.normalize_words("1200 block")


def test_strip_speaker_prefix() -> None:
    assert strip_speaker("SPEAKER_2: 911, what is it?") == "911, what is it?"
    assert strip_speaker("No prefix: here") == "No prefix: here"


# --- OCR chunk overlap -------------------------------------------------------------------


def test_strip_overlap_and_hypothesis_pages() -> None:
    assert strip_overlap(["a", "b", "c", "d"], ["c", "d", "e", "f"]) == ["e", "f"]
    assert strip_overlap(["a", "b"], ["x", "y"]) == ["x", "y"]
    chunks = [
        Chunk(index=0, text="SYNTHETIC one two\nthree four", locator={"kind": "pdf", "page": 1}),
        Chunk(index=1, text="three four five six", locator={"kind": "pdf", "page": 1}),
        Chunk(index=2, text="five six seven", locator={"kind": "pdf", "page": 2}),
    ]
    pages = hypothesis_pages(chunks)
    assert m.collapse_ws(pages[1]) == "SYNTHETIC one two three four five six"
    assert pages[2] == "five six seven"  # overlap is never removed across pages


# --- Diarization -------------------------------------------------------------------------


def test_best_speaker_mapping_is_one_to_one() -> None:
    pairs = [
        ("alice", "S1", 10.0),
        ("bob", "S2", 8.0),
        ("bob", "S1", 2.0),
        ("carol", "S2", 5.0),  # S2 cannot also be carol
    ]
    result = m.best_speaker_mapping(pairs)
    assert result.mapping == {"S1": "alice", "S2": "bob"}
    assert result.accuracy == pytest.approx(18 / 25)
    assert m.best_speaker_mapping([]).accuracy == 0.0


def test_overlap_pairs_weights_by_ms() -> None:
    gold = [
        {"start_ms": 0, "end_ms": 1000, "speaker": "a"},
        {"start_ms": 1000, "end_ms": 2000, "speaker": "b"},
    ]
    pairs, unattributed = overlap_pairs(gold, [(900, 1100, "S1"), (2500, 2600, "S2")])
    assert sorted(pairs) == [("a", "S1", 100.0), ("b", "S1", 100.0)]
    assert unattributed == 100.0


# --- Boxes -------------------------------------------------------------------------------


def test_iou() -> None:
    assert m.iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert m.iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert m.iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(50 / 150)
    assert m.interval_iou((0, 10), (5, 15)) == pytest.approx(5 / 15)


def test_match_greedy_and_ap() -> None:
    labels = [m.ScoredBox("f1", (0, 0, 10, 10)), m.ScoredBox("f2", (0, 0, 10, 10))]
    preds = [
        m.ScoredBox("f1", (0, 0, 10, 10), 0.9),  # TP
        m.ScoredBox("f1", (1, 1, 10, 10), 0.8),  # duplicate of a matched label: FP
        m.ScoredBox("f2", (50, 50, 60, 60), 0.7),  # wrong place: FP
        m.ScoredBox("f3", (0, 0, 10, 10), 0.6),  # unlabeled frame: FP
    ]
    assert m.match_greedy(labels, preds) == [True, False, False, False]
    # Precision/recall curve: (0.5, 1.0) then only FPs -> AP = 0.5.
    assert m.average_precision(labels, preds) == pytest.approx(0.5)
    perfect = [m.ScoredBox("f1", (0, 0, 10, 10), 0.9), m.ScoredBox("f2", (0, 0, 10, 10), 0.8)]
    assert m.average_precision(labels, perfect) == pytest.approx(1.0)
    assert m.average_precision([], perfect) == 0.0


def test_track_box_at_uses_nearest_point_within_tolerance() -> None:
    track = {"points": [{"ms": 0, "bbox": [0, 0, 1, 1]}, {"ms": 200, "bbox": [2, 2, 3, 3]}]}
    assert track_box_at(track, 180, 100) == [2, 2, 3, 3]
    assert track_box_at(track, 900, 100) is None


# --- Entity resolution -------------------------------------------------------------------


def test_pairwise_scores() -> None:
    gold = {"a": "P1", "b": "P1", "c": "P1", "d": "P2"}
    pred = {"a": "x", "b": "x", "c": "y", "d": "y", "e": "y"}  # e is unmapped: ignored
    s = m.pairwise(gold, pred)
    # gold pairs: ab ac bc; pred pairs among mapped: ab, cd
    assert (s.tp, s.fp, s.fn) == (1, 1, 2)
    assert s.precision == 0.5
    assert s.recall == pytest.approx(1 / 3)
    assert s.f1 == pytest.approx(0.4)


def test_connected_components() -> None:
    comp = m.connected_components(["a", "b", "c", "d"], [("a", "b"), ("c", "b")])
    assert comp["a"] == comp["b"] == comp["c"]
    assert comp["d"] == "d"


def test_story_assignment_rules() -> None:
    from caselens_data_gen.story import load_story
    from caselens_evals.components.er import assign, build_index

    story = load_story("riverside_lot")
    ix = build_index(story)
    assert assign(ix, "person", "Elina Ruiz", "elina ruiz", {}, story) == "p_witness_ruiz"
    assert assign(ix, "person", "Iverson", "iverson", {}, story) == "o_det_records"
    assert assign(ix, "person", "Person 12", "person 12", {}, story) is None
    assert assign(ix, "vehicle", "SYN482I", "SYN482I", {}, story) == "v_gray_sedan"
    assert assign(ix, "vehicle", "car", "SYN48", {"plate": "SYN48"}, story) == "v_gray_sedan"
    assert assign(ix, "vehicle", "white truck", None, {}, story) == "v_victim_pickup"
    assert assign(ix, "phone", "+13605550117", "+13605550117", {}, story) == "ph_suspect"
    assert assign(ix, "location", "river lot", "river lot", {}, story) == "loc_riverside_lot"


# --- Gated run against the ingested dev case ---------------------------------------------


@pytest.mark.skipif(os.environ.get("CASELENS_HEAVY") != "1", reason="set CASELENS_HEAVY=1")
def test_components_against_dev_case(tmp_path: Path) -> None:
    from caselens_evals.components.__main__ import main

    assert main(["--results", str(tmp_path)]) == 0
    out = next(tmp_path.glob("components-*"))
    assert (out / "components.json").is_file()
    assert "| Component | Metric | Value |" in (out / "components.md").read_text("utf-8")
