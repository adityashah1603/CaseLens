"""Speaker diarization without gated models.

pyannote needs a gated Hugging Face token, so CaseLens embeds sliding windows over the speech
regions found by VAD with SpeechBrain ECAPA (speechbrain/spkrec-ecapa-voxceleb, free and not
gated), clusters them with average-linkage agglomerative clustering on cosine distance (the
distance threshold estimates the speaker count), folds clusters with too little speech to judge
(a lone "Yeah.") into the nearest real one, and gives each word the speaker of the windows
that overlap it most. Labels are SPEAKER_1, SPEAKER_2, ... in order of first appearance.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

from caselens_workers.audio_asr import SAMPLE_RATE, Word

MIN_WINDOW_MS = 500  # ECAPA gets unreliable on shorter crops; short regions are padded to this.


class Span(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class SpeakerWindow(Span):
    speaker: str


class Diarization(BaseModel):
    num_speakers: int = Field(ge=0)
    windows: list[SpeakerWindow]
    word_speakers: list[str]


def sliding_windows(
    regions: Sequence[Span], window_ms: int, hop_ms: int, total_ms: int | None = None
) -> list[Span]:
    """Windows of `window_ms` every `hop_ms` inside each region; the last one is aligned to the
    region end. Regions shorter than a window get one window, padded to MIN_WINDOW_MS."""
    out: list[Span] = []
    for r in regions:
        length = r.end_ms - r.start_ms
        if length <= window_ms:
            start, end = r.start_ms, r.end_ms
            if length < MIN_WINDOW_MS:
                pad = (MIN_WINDOW_MS - length + 1) // 2
                start, end = max(0, start - pad), end + pad
                if total_ms is not None:
                    end = min(end, total_ms)
            out.append(Span(start_ms=start, end_ms=end))
            continue
        starts = list(range(r.start_ms, r.end_ms - window_ms + 1, hop_ms))
        if starts[-1] + window_ms < r.end_ms:
            starts.append(r.end_ms - window_ms)
        out.extend(Span(start_ms=s, end_ms=s + window_ms) for s in starts)
    return out


def merge_spans(spans: Sequence[tuple[int, int]], gap_ms: int) -> list[Span]:
    """Sorted, merged spans; spans separated by less than `gap_ms` are joined."""
    out: list[Span] = []
    for start, end in sorted(spans):
        if out and start - out[-1].end_ms < gap_ms:
            out[-1] = Span(start_ms=out[-1].start_ms, end_ms=max(out[-1].end_ms, end))
        else:
            out.append(Span(start_ms=start, end_ms=end))
    return out


def _unit(embeddings: NDArray[np.float64]) -> NDArray[np.float64]:
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    unit: NDArray[np.float64] = embeddings / np.maximum(norms, 1e-12)
    return unit


def _covered_ms(spans: Sequence[Span]) -> int:
    total = 0
    for s in merge_spans([(w.start_ms, w.end_ms) for w in spans], 0):
        total += s.end_ms - s.start_ms
    return total


def absorb_small_clusters(
    labels: Sequence[int],
    embeddings: NDArray[np.float64],
    windows: Sequence[Span],
    min_speech_ms: int,
) -> list[int]:
    """Reassign every window of a cluster covering less than `min_speech_ms` of audio to the
    nearest (cosine, to the centroid) cluster that covers enough. Too little speech is not
    evidence of a new voice. Returns ids renumbered by first appearance."""
    if not labels:
        return []
    ids = sorted(set(labels))
    covered = {
        c: _covered_ms([w for w, lab in zip(windows, labels, strict=True) if lab == c]) for c in ids
    }
    keep = [c for c in ids if covered[c] >= min_speech_ms]
    if not keep or len(keep) == len(ids):
        return _renumber(labels)
    unit = _unit(embeddings)
    lab_arr = np.asarray(labels)
    centroids = _unit(np.stack([unit[lab_arr == c].mean(axis=0) for c in keep]))
    out = [
        lab if lab in keep else keep[int(np.argmax(centroids @ unit[i]))]
        for i, lab in enumerate(labels)
    ]
    return _renumber(out)


def _renumber(labels: Sequence[int]) -> list[int]:
    order: dict[int, int] = {}
    return [order.setdefault(int(c), len(order)) for c in labels]


def cluster_embeddings(embeddings: NDArray[np.float64], threshold: float) -> list[int]:
    """Average-linkage agglomerative clustering on cosine distance, stopping at `threshold`.
    Returns cluster ids renumbered 0, 1, ... in order of first appearance."""
    n = len(embeddings)
    if n == 0:
        return []
    if n == 1:
        return [0]
    from sklearn.cluster import AgglomerativeClustering

    raw = AgglomerativeClustering(
        n_clusters=None, distance_threshold=threshold, metric="cosine", linkage="average"
    ).fit_predict(_unit(embeddings))
    return _renumber([int(c) for c in raw])


def label_words(
    words: Sequence[Word], windows: Sequence[Span], window_labels: Sequence[int]
) -> list[int]:
    """Each word gets the label with the most window overlap (ties: the earlier window); a word
    no window touches takes the label of the nearest window."""
    if not windows:
        return [0] * len(words)
    starts = np.array([w.start_ms for w in windows], dtype=np.int64)
    ends = np.array([w.end_ms for w in windows], dtype=np.int64)
    centers = (starts + ends) / 2
    labels = np.asarray(window_labels, dtype=np.int64)
    out: list[int] = []
    for w in words:
        overlap = np.minimum(ends, max(w.end_ms, w.start_ms + 1)) - np.maximum(starts, w.start_ms)
        hit = overlap > 0
        if not hit.any():
            mid = (w.start_ms + w.end_ms) / 2
            out.append(int(labels[int(np.argmin(np.abs(centers - mid)))]))
            continue
        votes: dict[int, int] = {}
        for lab, ov in zip(labels[hit], overlap[hit], strict=True):
            votes[int(lab)] = votes.get(int(lab), 0) + int(ov)
        best = max(votes.values())
        first = next(int(lab) for lab in labels[hit] if votes[int(lab)] == best)
        out.append(first)
    return out


@lru_cache(maxsize=1)
def _ecapa(source: str, savedir: str) -> Any:
    from speechbrain.inference.speaker import EncoderClassifier
    from speechbrain.utils.fetching import LocalStrategy

    # Download straight into savedir (no symlinks: Windows without developer mode cannot make
    # them, and no second copy in the Hugging Face home cache).
    return EncoderClassifier.from_hparams(
        source=source,
        savedir=savedir,
        run_opts={"device": "cpu"},
        local_strategy=LocalStrategy.COPY_SKIP_CACHE,
    )


def embed_windows(
    wav: Path, windows: Sequence[Span], source: str, cache_dir: Path
) -> NDArray[np.float64]:
    """One ECAPA speaker embedding (192-dim) per window."""
    import soundfile
    import torch

    audio, sr = soundfile.read(str(wav), dtype="float32", always_2d=False)
    if sr != SAMPLE_RATE or audio.ndim != 1:
        raise ValueError(f"expected 16 kHz mono audio, got {sr} Hz, shape {audio.shape}")
    model = _ecapa(source, str(cache_dir / "speechbrain" / source.replace("/", "--")))
    vectors: list[NDArray[np.float64]] = []
    with torch.inference_mode():
        for w in windows:
            lo = w.start_ms * sr // 1000
            crop = audio[lo : max(w.end_ms * sr // 1000, lo + 1)]
            if crop.size < sr // 10:
                crop = np.pad(crop, (0, sr // 10 - crop.size))
            emb = model.encode_batch(torch.from_numpy(np.ascontiguousarray(crop))[None, :])
            vectors.append(emb.reshape(-1).double().numpy())
    return np.stack(vectors) if vectors else np.zeros((0, 192), dtype=np.float64)


def diarize_words(
    words: Sequence[Word],
    embeddings: NDArray[np.float64],
    windows: Sequence[Span],
    threshold: float,
    min_speech_ms: int,
) -> Diarization:
    labels = absorb_small_clusters(
        cluster_embeddings(embeddings, threshold), embeddings, windows, min_speech_ms
    )
    word_labels = label_words(words, windows, labels)
    # Name speakers by first appearance in the words (what a reader sees); clusters that won no
    # word come after.
    order: dict[int, str] = {}
    for lab in [*word_labels, *labels]:
        order.setdefault(lab, f"SPEAKER_{len(order) + 1}")
    return Diarization(
        num_speakers=len(order),
        windows=[
            SpeakerWindow(start_ms=w.start_ms, end_ms=w.end_ms, speaker=order[lab])
            for w, lab in zip(windows, labels, strict=True)
        ],
        word_speakers=[order[lab] for lab in word_labels],
    )
