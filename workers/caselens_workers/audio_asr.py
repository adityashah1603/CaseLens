"""Audio normalize + ASR helpers (ffmpeg, faster-whisper on CPU).

Heavy libraries are imported lazily so importing the audio pipeline stays cheap.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SAMPLE_RATE = 16_000


class AudioSettings(BaseSettings):
    """Audio-pipeline knobs from CASELENS_AUDIO_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="CASELENS_AUDIO_", extra="ignore")

    # base.en vs small.en measured on the SYNTHETIC demo audio: same WER class, base is ~3x faster.
    asr_model: str = "base.en"
    asr_beam_size: int = 5
    # Silero VAD (bundled with faster-whisper). Splitting at short pauses keeps whisper from
    # stretching a word across the silence between two speakers' turns.
    vad_min_silence_ms: int = 500
    vad_pad_ms: int = 200
    speaker_model: str = "speechbrain/spkrec-ecapa-voxceleb"
    window_ms: int = 1500
    hop_ms: int = 750
    # Diarization windows never straddle a pause of at least this length (VAD, tight padding).
    region_gap_ms: int = 250
    region_pad_ms: int = 30
    # Cosine-distance cut for average-linkage clustering of ECAPA embeddings.
    cluster_threshold: float = 0.7
    # A cluster covering less speech than this is folded into the nearest bigger one.
    min_speaker_ms: int = 2000
    max_turn_ms: int = 60_000
    max_turn_words: int = 300  # ~400 tokens


class Word(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str
    probability: float = Field(default=1.0, ge=0, le=1)


class AsrSegment(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str


class Transcript(BaseModel):
    model: str
    language: str
    duration_ms: int = Field(ge=0)
    segments: list[AsrSegment]
    words: list[Word]


def limit_threads(threads: int) -> None:
    """Cap math-library threads (torch, OpenMP) so a laptop stays responsive."""
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(threads))
    try:
        import torch
    except ImportError:  # pragma: no cover - torch is only needed for diarization
        return
    torch.set_num_threads(threads)


def normalize_audio(src: Path, dst: Path, threads: int = 2) -> int:
    """Convert any ffmpeg-readable audio/video to 16 kHz mono PCM WAV. Returns duration in ms."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is not on PATH")
    # Fixed argv (no shell); the only inputs are paths inside the job's workdir.
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-threads",
            str(threads),
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(dst),
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return wav_duration_ms(dst)


def wav_duration_ms(path: Path) -> int:
    import soundfile

    info = soundfile.info(str(path))
    frames: int = info.frames
    rate: int = info.samplerate
    return round(frames * 1000 / rate)


@lru_cache(maxsize=2)
def _whisper(model: str, cache_dir: str, threads: int) -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(
        model,
        device="cpu",
        compute_type="int8",
        cpu_threads=threads,
        num_workers=1,
        download_root=cache_dir,
    )


def detect_speech(wav: Path, min_silence_ms: int, pad_ms: int) -> list[tuple[int, int]]:
    """Speech regions (start_ms, end_ms) from Silero VAD."""
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    audio = decode_audio(str(wav), sampling_rate=SAMPLE_RATE)
    opts = VadOptions(min_silence_duration_ms=min_silence_ms, speech_pad_ms=pad_ms)
    return [
        (int(r["start"]) * 1000 // SAMPLE_RATE, int(r["end"]) * 1000 // SAMPLE_RATE)
        for r in get_speech_timestamps(audio, opts, sampling_rate=SAMPLE_RATE)
    ]


def transcribe(
    wav: Path,
    model: str,
    cache_dir: Path,
    threads: int,
    beam_size: int = 5,
    vad_min_silence_ms: int = 500,
    vad_pad_ms: int = 200,
) -> Transcript:
    """English ASR with word timestamps, run on VAD speech regions."""
    engine = _whisper(model, str(cache_dir / "faster-whisper"), threads)
    segments_iter, info = engine.transcribe(
        str(wav),
        language="en",
        word_timestamps=True,
        beam_size=beam_size,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": vad_min_silence_ms,
            "speech_pad_ms": vad_pad_ms,
        },
    )
    segments: list[AsrSegment] = []
    words: list[Word] = []
    for seg in segments_iter:
        segments.append(
            AsrSegment(start_ms=_ms(seg.start), end_ms=_ms(seg.end), text=seg.text.strip())
        )
        for w in seg.words or []:
            text = w.word.strip()
            if not text:
                continue
            start = _ms(w.start)
            words.append(
                Word(
                    start_ms=start,
                    end_ms=max(start, _ms(w.end)),
                    text=text,
                    probability=min(1.0, max(0.0, float(w.probability))),
                )
            )
    return Transcript(
        model=model,
        language="en",
        duration_ms=_ms(info.duration),
        segments=segments,
        words=words,
    )


def _ms(seconds: float) -> int:
    return max(0, round(float(seconds) * 1000))
