"""Audio pipeline tests. All data is SYNTHETIC: generated tones, made-up word lists, random
embeddings, and (heavy tests only) the generated demo-case audio."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from caselens_workers import audio_asr, audio_diarize, jobs
from caselens_workers.audio_asr import AsrSegment, Transcript, Word, normalize_audio
from caselens_workers.audio_diarize import (
    Span,
    absorb_small_clusters,
    cluster_embeddings,
    diarize_words,
    label_words,
    merge_spans,
    sliding_windows,
)
from caselens_workers.audio_segment import build_turns, split_turn, turns_to_chunks
from caselens_workers.chunks import AudioLocator
from caselens_workers.db import Conn
from caselens_workers.pipeline import Pipeline, run_job
from caselens_workers.pipelines import audio
from caselens_workers.settings import WorkerSettings
from caselens_workers.stages.embed import EMBEDDING_DIM, make_embed_stage
from caselens_workers.testing import SeededEvidence, as_role

REPO = Path(__file__).resolve().parents[2]
HEAVY = os.environ.get("CASELENS_HEAVY") == "1"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _words(spec: list[tuple[int, int, str]]) -> list[Word]:
    return [Word(start_ms=s, end_ms=e, text=t) for s, e, t in spec]


def _tone(path: Path, seconds: float = 1.0) -> None:
    """A SYNTHETIC 44.1 kHz stereo sine tone, so normalize has something to convert."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    sine = f"sine=frequency=440:sample_rate=44100:duration={seconds}"
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            sine,
            "-ac",
            "2",
            path,
        ],
        check=True,
        capture_output=True,
    )


# --- stage graph -------------------------------------------------------------------------------


def test_stage_graph() -> None:
    order, stage = [], audio.PIPELINE.first
    while stage is not None:
        order.append(stage)
        stage = audio.PIPELINE.next[stage]
    assert order == ["normalize", "asr", "diarize", "segment", "embed", "extract"]


# --- normalize ---------------------------------------------------------------------------------


@needs_ffmpeg
def test_normalize_to_16k_mono(tmp_path: Path) -> None:
    import soundfile

    src, dst = tmp_path / "tone.wav", tmp_path / "out.wav"
    _tone(src, 1.0)
    duration = normalize_audio(src, dst, threads=1)
    info = soundfile.info(str(dst))
    assert (info.samplerate, info.channels, info.subtype) == (16_000, 1, "PCM_16")
    assert abs(duration - 1000) <= 5


# --- segment -----------------------------------------------------------------------------------


def test_build_turns_merges_consecutive_speakers() -> None:
    words = _words([(0, 100, "Hello."), (100, 200, "Hi"), (200, 300, "there."), (400, 500, "Ok.")])
    turns = build_turns(words, ["SPEAKER_1", "SPEAKER_2", "SPEAKER_2", "SPEAKER_1"])
    assert [(t.speaker, t.text, t.start_ms, t.end_ms) for t in turns] == [
        ("SPEAKER_1", "Hello.", 0, 100),
        ("SPEAKER_2", "Hi there.", 100, 300),
        ("SPEAKER_1", "Ok.", 400, 500),
    ]
    with pytest.raises(ValueError):
        build_turns(words, ["SPEAKER_1"])


def test_split_long_turn_at_sentence_boundary() -> None:
    # 10 words of 1 s each; sentences end at words 2 and 6.
    texts = ["a", "b", "c.", "d", "e", "f", "g.", "h", "i", "j"]
    words = _words([(i * 1000, i * 1000 + 900, t) for i, t in enumerate(texts)])
    (turn,) = build_turns(words, ["SPEAKER_1"] * 10)
    pieces = split_turn(turn, max_ms=60_000, max_words=5)
    assert [p.text for p in pieces] == ["a b c.", "d e f g.", "h i j"]
    by_time = split_turn(turn, max_ms=4500, max_words=300)
    assert [p.text for p in by_time] == ["a b c.", "d e f g.", "h i j"]
    no_punct = build_turns(_words([(i * 100, i * 100 + 90, "w") for i in range(7)]), ["S"] * 7)
    assert [len(p.words) for p in split_turn(no_punct[0], 60_000, 3)] == [3, 3, 1]


def test_chunks_have_audio_locators_and_wall_clock() -> None:
    words = _words([(1200, 1500, "Gray"), (1500, 1900, "car."), (2500, 3000, "Okay.")])
    turns = build_turns(words, ["SPEAKER_1", "SPEAKER_1", "SPEAKER_2"])
    start = datetime(2026, 3, 14, 22, 48, 40, tzinfo=UTC)
    chunks = turns_to_chunks(turns, start, 60_000, 300)
    assert [c.text for c in chunks] == ["SPEAKER_1: Gray car.", "SPEAKER_2: Okay."]
    assert chunks[0].locator == AudioLocator(start_ms=1200, end_ms=1900, speaker="SPEAKER_1")
    assert chunks[1].t_start == start + timedelta(milliseconds=2500)
    assert chunks[1].t_end == start + timedelta(milliseconds=3000)
    assert [c.chunk_index for c in chunks] == [0, 1]
    assert turns_to_chunks(turns, None, 60_000, 300)[0].t_start is None


# --- diarization helpers -----------------------------------------------------------------------


def test_merge_spans_and_sliding_windows() -> None:
    regions = merge_spans([(5000, 5200), (0, 1000), (1100, 4000)], gap_ms=250)
    assert regions == [Span(start_ms=0, end_ms=4000), Span(start_ms=5000, end_ms=5200)]
    windows = sliding_windows(regions, window_ms=1500, hop_ms=750, total_ms=5300)
    assert [(w.start_ms, w.end_ms) for w in windows] == [
        (0, 1500),
        (750, 2250),
        (1500, 3000),
        (2250, 3750),
        (2500, 4000),  # last window aligned to the region end
        (4850, 5300),  # short region padded toward MIN_WINDOW_MS, clipped to the audio
    ]


def _blobs(sizes: list[int], seed: int = 7) -> tuple[np.ndarray[Any, Any], list[int]]:
    rng = np.random.default_rng(seed)
    centers = np.eye(len(sizes), 16) * 5.0
    rows, truth = [], []
    for k, n in enumerate(sizes):
        rows.append(centers[k] + rng.normal(scale=0.3, size=(n, 16)))
        truth += [k] * n
    return np.vstack(rows), truth


def test_cluster_embeddings_estimates_speaker_count() -> None:
    emb, truth = _blobs([6, 4, 3])
    order = np.random.default_rng(1).permutation(len(truth))
    labels = cluster_embeddings(emb[order], threshold=0.5)
    shuffled = [truth[i] for i in order]
    assert len(set(labels)) == 3
    assert [labels.index(k) for k in range(3)] == sorted(labels.index(k) for k in range(3))
    pairs = set(zip(labels, shuffled, strict=True))
    assert len(pairs) == 3  # a perfect 1:1 mapping
    assert cluster_embeddings(emb[:1], 0.5) == [0]
    assert cluster_embeddings(emb[:0], 0.5) == []


def test_absorb_small_clusters_folds_tiny_voices() -> None:
    emb, _ = _blobs([4, 4])
    stray = emb[0] + 0.05  # a lone short "Yeah." that clustered alone
    emb = np.vstack([emb, stray])
    windows = [Span(start_ms=i * 1500, end_ms=i * 1500 + 1500) for i in range(8)]
    windows.append(Span(start_ms=20_000, end_ms=20_500))
    labels = [0, 0, 0, 0, 1, 1, 1, 1, 2]
    assert absorb_small_clusters(labels, emb, windows, min_speech_ms=2000) == [0] * 4 + [1] * 4 + [
        0
    ]
    assert absorb_small_clusters(labels, emb, windows, min_speech_ms=400) == labels


def test_label_words_by_overlap() -> None:
    windows = [Span(start_ms=0, end_ms=1500), Span(start_ms=1000, end_ms=2500)]
    words = _words([(0, 400, "a"), (1100, 1450, "b"), (1400, 2000, "c"), (5000, 5100, "far")])
    assert label_words(words, windows, [0, 1]) == [0, 0, 1, 1]
    assert label_words(words, [], []) == [0, 0, 0, 0]


def test_diarize_words_names_by_first_appearance() -> None:
    emb, _ = _blobs([3, 3])
    emb = emb[[3, 4, 5, 0, 1, 2]]
    windows = [Span(start_ms=i * 1000, end_ms=i * 1000 + 1500) for i in range(6)]
    words = _words([(i * 1000 + 100, i * 1000 + 600, f"w{i}") for i in range(6)])
    result = diarize_words(words, emb, windows, threshold=0.5, min_speech_ms=0)
    assert result.num_speakers == 2
    assert result.word_speakers == ["SPEAKER_1"] * 3 + ["SPEAKER_2"] * 3


# --- stages through the job framework (DB, fake models) ----------------------------------------


def _drain(conn: Conn, pipe: Pipeline, ev: SeededEvidence, settings: WorkerSettings) -> list[str]:
    out = []
    while (job := jobs.claim_next(conn, set(pipe.stages), 3, 1800)) is not None:
        status = run_job(conn, job, {"audio": pipe}, ev.store, settings, jobs.PostgresQueue())
        out.append(f"{job.stage}:{status}")
    return out


def _test_pipeline() -> Pipeline:
    def fake_embed(texts: object) -> list[list[float]]:
        return [[0.1] * EMBEDDING_DIM for _ in texts]  # type: ignore[attr-defined]

    stages = dict(audio.PIPELINE.stages)
    stages["embed"] = make_embed_stage(fake_embed)
    return Pipeline(first=audio.PIPELINE.first, stages=stages, next=audio.PIPELINE.next)


@pytest.fixture
def fake_models(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Replace ASR, VAD and speaker embeddings with SYNTHETIC stand-ins (no downloads)."""

    def transcribe(wav: Path, *args: object) -> Transcript:
        words = _words([(100, 400, "Gray"), (400, 800, "sedan."), (1200, 1600, "Copy.")])
        return Transcript(
            model="fake",
            language="en",
            duration_ms=audio_asr.wav_duration_ms(wav),
            segments=[AsrSegment(start_ms=100, end_ms=1600, text="Gray sedan. Copy.")],
            words=words,
        )

    def embed_windows(wav: Path, windows: list[Span], *args: object) -> np.ndarray[Any, Any]:
        return np.array([[1.0, 0.0] if w.start_ms < 900 else [0.0, 1.0] for w in windows])

    monkeypatch.setattr(audio_asr, "transcribe", transcribe)
    monkeypatch.setattr(audio_asr, "detect_speech", lambda *a: [(100, 800), (1200, 1600)])
    monkeypatch.setattr(audio_diarize, "embed_windows", embed_windows)
    yield


@needs_ffmpeg
@pytest.mark.db
@pytest.mark.usefixtures("fake_models")
def test_pipeline_writes_speaker_turn_chunks(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    worker_settings: WorkerSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CASELENS_AUDIO_MIN_SPEAKER_MS", "0")
    tone = tmp_path / "SYNTHETIC_tone.wav"
    _tone(tone, 2.0)
    ev = seed_evidence(tone, "audio", recorded_at="2026-03-14T22:48:40-07:00")
    jobs.PostgresQueue().enqueue(owner_conn, ev.case_id, ev.evidence_id, "normalize")
    as_role(owner_conn, "caselens_worker")
    pipe = _test_pipeline()
    statuses = _drain(owner_conn, pipe, ev, worker_settings)
    assert statuses == [
        f"{s}:succeeded" for s in ["normalize", "asr", "diarize", "segment", "embed", "extract"]
    ]
    rows = owner_conn.execute(
        "SELECT chunk_index, text, locator, t_start, modality FROM public.chunks "
        "WHERE evidence_id = %s ORDER BY chunk_index",
        (ev.evidence_id,),
    ).fetchall()
    start = datetime.fromisoformat("2026-03-14T22:48:40-07:00")
    assert [(r[0], r[1], r[4]) for r in rows] == [
        (0, "SPEAKER_1: Gray sedan.", "audio"),
        (1, "SPEAKER_2: Copy.", "audio"),
    ]
    assert rows[0][2] == {"kind": "audio", "start_ms": 100, "end_ms": 800, "speaker": "SPEAKER_1"}
    assert rows[1][3] == start + timedelta(milliseconds=1200)

    # Re-running segment replaces chunks instead of duplicating them.
    assert jobs.requeue(owner_conn, ev.evidence_id, "segment")
    _drain(owner_conn, pipe, ev, worker_settings)
    count = owner_conn.execute(
        "SELECT count(*) FROM public.chunks WHERE evidence_id = %s", (ev.evidence_id,)
    ).fetchone()
    assert count == (2,)


# --- real models (CASELENS_HEAVY=1) -----------------------------------------------------------


def _demo_audio() -> tuple[Path, Path] | None:
    for case in ("harbor_street", "riverside_lot"):
        case_dir = REPO / "data_gen" / "output" / case
        files = sorted((case_dir / "audio").glob("*.wav")) if (case_dir / "audio").is_dir() else []
        if files:
            return case_dir, files[0]
    return None


@pytest.mark.skipif(not HEAVY, reason="set CASELENS_HEAVY=1 to run real ASR + diarization")
@needs_ffmpeg
@pytest.mark.db
def test_real_audio_end_to_end(
    owner_conn: Conn,
    seed_evidence: Callable[..., SeededEvidence],
    worker_settings: WorkerSettings,
) -> None:
    found = _demo_audio()
    if found is None:
        pytest.skip("no generated demo audio (run data_gen)")
    case_dir, wav = found
    settings = worker_settings.model_copy(update={"model_cache_dir": REPO / ".cache" / "models"})
    ev = seed_evidence(wav, "audio", recorded_at="2026-03-14T22:48:40-07:00")
    jobs.PostgresQueue().enqueue(owner_conn, ev.case_id, ev.evidence_id, "normalize")
    as_role(owner_conn, "caselens_worker")
    statuses = _drain(owner_conn, _test_pipeline(), ev, settings)
    assert all(s.endswith(":succeeded") for s in statuses), statuses

    rows = owner_conn.execute(
        "SELECT text, locator FROM public.chunks WHERE evidence_id = %s ORDER BY chunk_index",
        (ev.evidence_id,),
    ).fetchall()
    gold = json.loads(wav.with_suffix(".transcript.json").read_text(encoding="utf-8"))
    speakers = {r[1]["speaker"] for r in rows}
    assert len(speakers) == len(gold["speakers"])
    # Every answer-key fact on this file has a chunk overlapping its time range.
    facts = json.loads((case_dir / "answer_key.json").read_text(encoding="utf-8"))["facts"]
    mine = [
        f["locator"]
        for f in facts
        if f["evidence"] == f"audio/{wav.name}" and f["locator"]["kind"] == "audio"
    ]
    assert mine
    for loc in mine:
        assert any(
            r[1]["start_ms"] < loc["end_ms"] and r[1]["end_ms"] > loc["start_ms"] for r in rows
        ), loc
