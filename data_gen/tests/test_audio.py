"""Tests for the SYNTHETIC audio generator (fake tone synthesizer; Piper only if voices exist)."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from caselens_data_gen.audio import ToneSynthesizer, build_scripts, generate
from caselens_data_gen.audio.script import NOTICE_LINE, NOTICE_SPEAKER
from caselens_data_gen.audio.synth import SAMPLE_RATE, PiperSynthesizer, missing_voices
from caselens_data_gen.common import GeneratedFile
from caselens_data_gen.story import Story, load_story


@pytest.fixture(scope="module")
def riverside() -> Story:
    return load_story("riverside_lot")


@pytest.fixture(scope="module")
def produced(riverside: Story, tmp_path_factory: pytest.TempPathFactory) -> list[GeneratedFile]:
    case_dir = tmp_path_factory.mktemp("riverside_lot")
    return generate(riverside, case_dir, synthesizer=ToneSynthesizer())


def _transcript(f: GeneratedFile) -> dict[str, object]:
    path = f.path.with_name(f.path.stem + ".transcript.json")
    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _segments(f: GeneratedFile) -> list[dict[str, object]]:
    segments = _transcript(f)["segments"]
    assert isinstance(segments, list)
    return segments


def _by_id(produced: list[GeneratedFile]) -> dict[str, GeneratedFile]:
    return {f.path.stem: f for f in produced}


def test_harbor_street_has_no_audio(tmp_path: Path) -> None:
    assert generate(load_story("harbor_street"), tmp_path, synthesizer=ToneSynthesizer()) == []


def test_files_and_wav_format(produced: list[GeneratedFile], riverside: Story) -> None:
    files = _by_id(produced)
    assert set(files) == {"aud_911", "aud_interview"}
    plan = {p.id: p for p in riverside.evidence_plan}
    for eid, f in files.items():
        assert f.evidence_type == "audio"
        assert f.recorded_at == plan[eid].recorded_at.isoformat()
        with wave.open(str(f.path), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == SAMPLE_RATE
            duration_ms = handle.getnframes() * 1000 / SAMPLE_RATE
        raw = f.path.read_bytes()
        assert b"LIST" in raw and b"SYNTHETIC DATA" in raw
        target = (plan[eid].duration_s or 0) * 1000
        assert abs(duration_ms - target) <= 0.15 * target
        assert abs(_transcript(f)["duration_ms"] - duration_ms) <= 1  # type: ignore[operator]


def test_segments_ordered_and_notice_first(produced: list[GeneratedFile], riverside: Story) -> None:
    plan = {p.id: p for p in riverside.evidence_plan}
    for f in produced:
        segments = _segments(f)
        duration = _transcript(f)["duration_ms"]
        assert isinstance(duration, int)
        first = segments[0]
        assert first["speaker"] == NOTICE_SPEAKER and first["text"] == NOTICE_LINE
        previous_end = 0
        for seg in segments:
            start, end = seg["start_ms"], seg["end_ms"]
            assert isinstance(start, int) and isinstance(end, int)
            assert previous_end <= start < end <= duration
            previous_end = end
        speakers = {s["speaker"] for s in segments} - {NOTICE_SPEAKER}
        assert speakers == set(plan[f.path.stem].speaker_ids)


def test_key_lines_spoken_by_right_speaker(produced: list[GeneratedFile], riverside: Story) -> None:
    files = _by_id(produced)
    for item in riverside.evidence_plan:
        if item.type != "audio":
            continue
        segments = _segments(files[item.id])
        for line in item.key_lines:
            assert any(
                s["speaker"] == line.speaker_id and line.text in str(s["text"]) for s in segments
            ), line.text


def test_conflict_quotes_located_in_suspect_segments(produced: list[GeneratedFile]) -> None:
    interview = _by_id(produced)["aud_interview"]
    segments = _segments(interview)
    expected = {"cf_02": ["never even got out", "Before 10:40"], "cf_03": ["I was by myself"]}
    for conflict, phrases in expected.items():
        located = [fl for fl in interview.facts if fl.fact_id == conflict]
        assert located
        texts = []
        for fl in located:
            assert fl.locator["speaker"] == "p_suspect"
            seg = next(s for s in segments if s["start_ms"] == fl.locator["start_ms"])
            assert seg["end_ms"] == fl.locator["end_ms"] and seg["speaker"] == "p_suspect"
            texts.append(str(seg["text"]))
        for phrase in phrases:
            assert any(phrase in t for t in texts), phrase


def test_covered_facts_have_exact_locators(produced: list[GeneratedFile], riverside: Story) -> None:
    files = _by_id(produced)
    for item in riverside.evidence_plan:
        if item.type != "audio":
            continue
        f = files[item.id]
        segments = _segments(f)
        duration = _transcript(f)["duration_ms"]
        assert isinstance(duration, int)
        ids = {fl.fact_id for fl in f.facts}
        assert set(item.covers_events) <= ids
        assert set(item.covers_entities) <= ids
        for fl in f.facts:
            loc = fl.locator
            assert loc["kind"] == "audio"
            assert 0 <= loc["start_ms"] < loc["end_ms"] <= duration
            assert any(
                s["start_ms"] == loc["start_ms"]
                and s["end_ms"] == loc["end_ms"]
                and s["speaker"] == loc["speaker"]
                for s in segments
            )


def test_tone_render_is_deterministic(riverside: Story, tmp_path: Path) -> None:
    a = generate(riverside, tmp_path / "a", synthesizer=ToneSynthesizer())
    b = generate(riverside, tmp_path / "b", synthesizer=ToneSynthesizer())
    for x, y in zip(a, b, strict=True):
        assert x.path.read_bytes() == y.path.read_bytes()


def test_piper_synthesizes_one_line(riverside: Story) -> None:
    pytest.importorskip("piper")
    voices = {spec.voice for s in build_scripts(riverside) for spec in s.voices.values()}
    if missing_voices(voices):
        pytest.skip("Piper voices not downloaded into data_gen/.voices")
    script = build_scripts(riverside)[0]
    synth = PiperSynthesizer()
    spec = script.voices[NOTICE_SPEAKER]
    first = synth.synthesize(NOTICE_LINE, spec)
    assert 1.5 * SAMPLE_RATE < first.size < 6 * SAMPLE_RATE
    assert (first == synth.synthesize(NOTICE_LINE, spec)).all()
