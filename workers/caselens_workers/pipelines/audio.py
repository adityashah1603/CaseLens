"""Audio pipeline: normalize -> asr -> diarize -> segment -> embed -> extract.

* normalize: ffmpeg to 16 kHz mono PCM WAV (artifact `audio.wav`), duration recorded.
* asr: faster-whisper on CPU (int8, 2 threads), English, word timestamps (`transcript.json`).
* diarize: SpeechBrain ECAPA embeddings over sliding windows + agglomerative clustering
  (`diarization.json`). pyannote is gated behind a Hugging Face token, so it is not used.
* segment: speaker-turn chunks with `AudioLocator(start_ms, end_ms, speaker)`, text prefixed with
  the speaker label, wall-clock t_start/t_end from `evidence.recorded_at`.

Transcribed speech is evidence content: data, never instructions.
"""

from __future__ import annotations

from pathlib import Path

from caselens_workers.audio_asr import AudioSettings, Transcript, limit_threads
from caselens_workers.audio_diarize import Diarization
from caselens_workers.chunks import replace_chunks
from caselens_workers.context import StageContext, StageResult
from caselens_workers.pipeline import Pipeline
from caselens_workers.stages.embed import embed
from caselens_workers.stages.extract import extract

AUDIO_ARTIFACT = "audio.wav"
TRANSCRIPT_ARTIFACT = "transcript.json"
DIARIZATION_ARTIFACT = "diarization.json"


def _normalized_wav(ctx: StageContext) -> Path:
    """The normalize stage's WAV, fetched into this job's workdir."""
    path = ctx.workdir / "normalized.wav"
    if not path.exists():
        path.write_bytes(ctx.get_artifact("normalize", AUDIO_ARTIFACT))
    return path


def normalize(ctx: StageContext) -> StageResult:
    from caselens_workers.audio_asr import normalize_audio

    dst = ctx.workdir / "normalized.wav"
    duration_ms = normalize_audio(ctx.original_path(), dst, ctx.settings.model_threads)
    ctx.put_artifact(AUDIO_ARTIFACT, dst.read_bytes(), "audio/wav")
    ctx.put_json("audio.json", {"duration_ms": duration_ms, "sample_rate": 16_000})
    return StageResult(summary={"duration_ms": duration_ms})


def asr(ctx: StageContext) -> StageResult:
    from caselens_workers.audio_asr import transcribe

    cfg = AudioSettings()
    limit_threads(ctx.settings.model_threads)
    wav = _normalized_wav(ctx)
    transcript = transcribe(
        wav,
        cfg.asr_model,
        ctx.settings.model_cache_dir,
        ctx.settings.model_threads,
        cfg.asr_beam_size,
        cfg.vad_min_silence_ms,
        cfg.vad_pad_ms,
    )
    ctx.put_json(TRANSCRIPT_ARTIFACT, transcript.model_dump(mode="json"))
    return StageResult(summary={"model": transcript.model, "words": len(transcript.words)})


def diarize(ctx: StageContext) -> StageResult:
    from caselens_workers.audio_asr import detect_speech
    from caselens_workers.audio_diarize import (
        diarize_words,
        embed_windows,
        merge_spans,
        sliding_windows,
    )

    cfg = AudioSettings()
    limit_threads(ctx.settings.model_threads)
    transcript = Transcript.model_validate(ctx.get_json("asr", TRANSCRIPT_ARTIFACT))
    wav = _normalized_wav(ctx)
    vad = detect_speech(wav, cfg.region_gap_ms, cfg.region_pad_ms)
    regions = merge_spans(vad, cfg.region_gap_ms)
    windows = sliding_windows(regions, cfg.window_ms, cfg.hop_ms, transcript.duration_ms)
    embeddings = embed_windows(wav, windows, cfg.speaker_model, ctx.settings.model_cache_dir)
    result = diarize_words(
        transcript.words, embeddings, windows, cfg.cluster_threshold, cfg.min_speaker_ms
    )
    ctx.put_json(DIARIZATION_ARTIFACT, result.model_dump(mode="json"))
    return StageResult(summary={"speakers": result.num_speakers, "windows": len(windows)})


def segment(ctx: StageContext) -> StageResult:
    from caselens_workers.audio_segment import build_turns, turns_to_chunks

    cfg = AudioSettings()
    transcript = Transcript.model_validate(ctx.get_json("asr", TRANSCRIPT_ARTIFACT))
    result = Diarization.model_validate(ctx.get_json("diarize", DIARIZATION_ARTIFACT))
    turns = build_turns(transcript.words, result.word_speakers)
    chunks = turns_to_chunks(turns, ctx.evidence.recorded_at, cfg.max_turn_ms, cfg.max_turn_words)
    replace_chunks(ctx, chunks)
    return StageResult(summary={"chunks": len(chunks), "turns": len(turns)})


PIPELINE = Pipeline(
    first="normalize",
    stages={
        "normalize": normalize,
        "asr": asr,
        "diarize": diarize,
        "segment": segment,
        "embed": embed,
        "extract": extract,
    },
    next={
        "normalize": "asr",
        "asr": "diarize",
        "diarize": "segment",
        "segment": "embed",
        "embed": "extract",
        "extract": None,
    },
)
