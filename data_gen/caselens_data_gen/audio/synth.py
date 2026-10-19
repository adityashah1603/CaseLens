"""Speech synthesizers for the SYNTHETIC audio generator.

``PiperSynthesizer`` runs local Piper TTS voices (free, offline). ``ToneSynthesizer`` is a
deterministic stand-in (a soft tone whose length scales with the text) used by the fast tests.
Both return mono float64 audio at ``SAMPLE_RATE`` with leading and trailing silence trimmed.
"""

from __future__ import annotations

import os
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np
import numpy.typing as npt

from caselens_data_gen.audio.script import VoiceSpec

if TYPE_CHECKING:
    from piper import PiperVoice

SAMPLE_RATE = 16_000
SENTENCE_GAP_S = 0.22
Audio = npt.NDArray[np.float64]

DEFAULT_VOICES_DIR = Path(__file__).resolve().parents[2] / ".voices"
VOICES_ENV = "CASELENS_VOICES_DIR"


class Synthesizer(Protocol):
    name: str

    def synthesize(self, text: str, voice: VoiceSpec) -> Audio: ...


def resample(audio: Audio, src_rate: int, dst_rate: int = SAMPLE_RATE) -> Audio:
    """Band-limited FFT resample (deterministic, no extra dependency)."""
    if src_rate == dst_rate or audio.size == 0:
        return audio.astype(np.float64)
    n_out = round(audio.size * dst_rate / src_rate)
    spectrum = np.fft.rfft(audio)
    keep = n_out // 2 + 1
    if keep <= spectrum.size:
        spectrum = spectrum[:keep]
    else:
        spectrum = np.concatenate([spectrum, np.zeros(keep - spectrum.size, dtype=complex)])
    out: Audio = np.fft.irfft(spectrum, n_out) * (n_out / audio.size)
    return out


def trim_silence(audio: Audio, threshold: float = 0.01, margin_s: float = 0.03) -> Audio:
    loud = np.flatnonzero(np.abs(audio) > threshold * max(float(np.abs(audio).max()), 1e-9))
    if loud.size == 0:
        return audio
    margin = int(margin_s * SAMPLE_RATE)
    start = max(int(loud[0]) - margin, 0)
    end = min(int(loud[-1]) + margin + 1, audio.size)
    return audio[start:end]


def normalize(audio: Audio, peak: float) -> Audio:
    top = float(np.abs(audio).max()) if audio.size else 0.0
    return audio if top == 0.0 else audio * (peak / top)


def voices_dir() -> Path:
    return Path(os.environ.get(VOICES_ENV, str(DEFAULT_VOICES_DIR)))


def missing_voices(names: set[str], directory: Path | None = None) -> list[str]:
    base = directory or voices_dir()
    return sorted(n for n in names if not (base / f"{n}.onnx").is_file())


class PiperSynthesizer:
    """Local Piper TTS. Noise scales are 0 so the same text always yields the same samples."""

    name = "piper"

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or voices_dir()
        self._voices: dict[str, PiperVoice] = {}

    def _voice(self, name: str) -> PiperVoice:
        if name not in self._voices:
            from piper import PiperVoice

            path = self.directory / f"{name}.onnx"
            if not path.is_file():
                raise FileNotFoundError(
                    f"Piper voice {name!r} not found in {self.directory}; run "
                    f"`python -m piper.download_voices {name}` from that folder"
                )
            self._voices[name] = PiperVoice.load(path)
        return self._voices[name]

    def synthesize(self, text: str, voice: VoiceSpec) -> Audio:
        from piper.config import SynthesisConfig

        model = self._voice(voice.voice)
        config = SynthesisConfig(
            length_scale=voice.length_scale, noise_scale=0.0, noise_w_scale=0.0
        )
        rate = model.config.sample_rate
        gap = np.zeros(int(SENTENCE_GAP_S * rate))
        parts: list[Audio] = []
        for chunk in model.synthesize(text, config):
            if parts:
                parts.append(gap)
            parts.append(np.asarray(chunk.audio_float_array, dtype=np.float64))
        if not parts:
            raise ValueError(f"Piper produced no audio for {text!r}")
        return trim_silence(resample(np.concatenate(parts), rate))


class ToneSynthesizer:
    """Deterministic fake: a soft voice-pitched tone, ``ms_per_char`` long per character."""

    name = "tone"

    def __init__(self, ms_per_char: float = 65.0) -> None:
        self.ms_per_char = ms_per_char

    def synthesize(self, text: str, voice: VoiceSpec) -> Audio:
        n = max(int(len(text) * self.ms_per_char * voice.length_scale * SAMPLE_RATE / 1000), 1)
        freq = 110.0 + zlib.crc32(voice.voice.encode()) % 160
        t = np.arange(n) / SAMPLE_RATE
        envelope = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.02)
        tone: Audio = 0.5 * envelope * np.sin(2 * np.pi * freq * t)
        return tone
