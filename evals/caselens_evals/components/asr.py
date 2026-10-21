"""ASR word error rate and diarization accuracy on the two SYNTHETIC recordings.

Gold: data_gen's `<clip>.transcript.json` (the exact text each synthetic voice spoke, with
segment times and speaker ids). Hypotheses: (a) the ingested audio chunks in chunk order with the
"SPEAKER_n:" prefix removed, (b) the stored asr artifact `transcript.json` (word list).
Diarization: each ASR word (start/end ms, speaker from the stored `diarization.json`) overlaps
gold segments; the weight is the overlap in ms, and the score is the best one-to-one mapping of
predicted to gold speakers (accuracy = mapped overlap / all overlap).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from caselens_evals.components.metrics import (
    best_speaker_mapping,
    normalize_words,
    raw_words,
    wer,
)
from caselens_evals.components.sources import CaseSource, Chunk

CLIPS = ("aud_911", "aud_interview")
_SPEAKER_PREFIX = re.compile(r"^\s*[A-Z][A-Z_]*_?\d+:\s*")


def strip_speaker(text: str) -> str:
    """'SPEAKER_2: 911, what is ...' -> '911, what is ...'."""
    return _SPEAKER_PREFIX.sub("", text, count=1)


def gold_transcript(case_dir: Path, clip: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (case_dir / "audio" / f"{clip}.transcript.json").read_text(encoding="utf-8")
    )
    return data


def gold_text(gold: dict[str, Any]) -> str:
    segments = sorted(gold["segments"], key=lambda s: s["start_ms"])
    return " ".join(str(s["text"]) for s in segments)


def chunk_text(chunks: Sequence[Chunk]) -> str:
    return " ".join(strip_speaker(c.text) for c in sorted(chunks, key=lambda c: c.index))


def _wer_pair(reference: str, hypothesis: str) -> dict[str, Any]:
    ref_n, hyp_n = normalize_words(reference), normalize_words(hypothesis)
    return {
        "ref_words": len(ref_n),
        "hyp_words": len(hyp_n),
        "wer_raw": round(wer(raw_words(reference), raw_words(hypothesis)), 4),
        "wer_normalized": round(wer(ref_n, hyp_n), 4),
    }


def overlap_pairs(
    gold_segments: Sequence[dict[str, Any]],
    spans: Sequence[tuple[int, int, str]],
) -> tuple[list[tuple[str, str, float]], float]:
    """(gold, predicted, overlap ms) per (span, gold segment) overlap; plus span ms that overlap
    no gold segment (speech the ASR found outside any scripted segment)."""
    pairs: list[tuple[str, str, float]] = []
    unattributed = 0.0
    for start, end, speaker in spans:
        covered = 0.0
        for seg in gold_segments:
            ov = min(end, int(seg["end_ms"])) - max(start, int(seg["start_ms"]))
            if ov > 0:
                pairs.append((str(seg["speaker"]), speaker, float(ov)))
                covered += ov
        unattributed += max(0.0, (end - start) - covered)
    return pairs, unattributed


def _diarization(src: CaseSource, filename: str, gold: dict[str, Any]) -> dict[str, Any]:
    transcript = src.artifact_json(filename, "asr", "transcript.json")
    diar = src.artifact_json(filename, "diarize", "diarization.json")
    spans: list[tuple[int, int, str]]
    if transcript and diar and len(diar["word_speakers"]) == len(transcript["words"]):
        source = "asr/transcript.json words + diarize/diarization.json word_speakers"
        spans = [
            (int(w["start_ms"]), int(w["end_ms"]), str(s))
            for w, s in zip(transcript["words"], diar["word_speakers"], strict=True)
        ]
    else:
        source = "chunk locators (speaker turn spans)"
        spans = [
            (int(c.locator["start_ms"]), int(c.locator["end_ms"]), str(c.locator.get("speaker")))
            for c in src.chunks(filename)
        ]
    pairs, unattributed = overlap_pairs(gold["segments"], spans)
    result = best_speaker_mapping(pairs)
    return {
        "source": source,
        "accuracy": round(result.accuracy, 4),
        "mapping": result.mapping,
        "gold_speakers": len({str(s["speaker"]) for s in gold["segments"]}),
        "predicted_speakers": len({s for _, _, s in spans}),
        "scored_ms": round(result.total),
        "unattributed_ms": round(unattributed),
    }


def evaluate(src: CaseSource, case_dir: Path) -> dict[str, Any]:
    clips: dict[str, Any] = {}
    for clip in CLIPS:
        filename = f"{clip}.wav"
        gold = gold_transcript(case_dir, clip)
        reference = gold_text(gold)
        entry: dict[str, Any] = {"chunks": _wer_pair(reference, chunk_text(src.chunks(filename)))}
        transcript = src.artifact_json(filename, "asr", "transcript.json")
        if transcript:
            words = " ".join(str(w["text"]) for w in transcript["words"])
            entry["asr_artifact"] = _wer_pair(reference, words)
            entry["asr_model"] = transcript.get("model")
        entry["diarization"] = _diarization(src, filename, gold)
        clips[filename] = entry
    return {
        "clips": clips,
        "method": (
            "WER = word-level Levenshtein(gold, hyp) / gold words. gold = data_gen "
            "<clip>.transcript.json segments in time order (incl. the spoken SYNTHETIC notice). "
            "wer_raw: lowercase, punctuation removed, digits as written; wer_normalized: also "
            "numbers spelled out the way they are read (911 -> nine eleven, 1214 -> twelve "
            "fourteen, 16th -> sixteenth) and apostrophes dropped. Diarization accuracy: ASR word "
            "spans x gold segments weighted by overlap ms, best one-to-one predicted->gold "
            "speaker mapping; unattributed_ms = word time outside every gold segment (unscored)."
        ),
    }
