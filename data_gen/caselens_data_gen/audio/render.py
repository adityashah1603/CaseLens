"""Lay out, mix and write one SYNTHETIC recording plus its gold transcript sidecar."""

from __future__ import annotations

import itertools
import json
import struct
import wave
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from caselens_data_gen.audio.script import NOTICE_SPEAKER, RecordingScript
from caselens_data_gen.audio.synth import SAMPLE_RATE, Audio, Synthesizer, normalize
from caselens_data_gen.common import SYNTHETIC_NOTICE

LEAD_IN_MS = 400
TAIL_MS = 900
MIN_GAP_MS = 150.0
MAX_GAP_MS = 3000.0


@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int
    speaker: str
    text: str
    facts: tuple[str, ...]


@dataclass(frozen=True)
class Rendered:
    audio: Audio
    segments: list[Segment]

    @property
    def duration_ms(self) -> int:
        return round(self.audio.size * 1000 / SAMPLE_RATE)


def _ms_to_n(ms: float) -> int:
    return round(ms * SAMPLE_RATE / 1000)


def layout(script: RecordingScript, speech_ms: list[int]) -> tuple[list[Segment], int]:
    """Place turns back to back, spreading silence so the total lands on ``target_ms``.

    The silence before turn i is ``pause_weight_i * unit``; ``unit`` is solved from the target
    and clamped, so very long or short speech can still miss it.
    """
    weights = [t.pause_weight for t in script.turns[1:]]
    spare = script.target_ms - sum(speech_ms) - LEAD_IN_MS - TAIL_MS
    unit = min(max(spare / sum(weights), MIN_GAP_MS), MAX_GAP_MS) if weights else 0.0
    segments: list[Segment] = []
    cursor = float(LEAD_IN_MS)
    for i, (turn, length) in enumerate(zip(script.turns, speech_ms, strict=True)):
        if i:
            cursor += turn.pause_weight * unit
        start = round(cursor)
        segments.append(Segment(start, start + length, turn.speaker, turn.text, turn.facts))
        cursor = float(start + length)
    return segments, round(cursor) + TAIL_MS


def _rng(script: RecordingScript) -> np.random.Generator:
    return np.random.default_rng(zlib.crc32(script.evidence_id.encode()))


def _lowpass_noise(rng: np.random.Generator, n: int, cutoff_hz: float) -> Audio:
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
    spectrum *= 1.0 / (1.0 + (freqs / cutoff_hz) ** 2)
    out: Audio = np.fft.irfft(spectrum, n)
    return out / (float(np.std(out)) or 1.0)


def _bandlimit(audio: Audio, low_hz: float, high_hz: float) -> Audio:
    spectrum = np.fft.rfft(audio)
    freqs = np.fft.rfftfreq(audio.size, 1 / SAMPLE_RATE)
    mask = 1.0 / (1.0 + (low_hz / np.maximum(freqs, 1.0)) ** 4)
    mask *= 1.0 / (1.0 + (freqs / high_hz) ** 8)
    out: Audio = np.fft.irfft(spectrum * mask, audio.size)
    return out


def _tone(n: int, freq: float, amp: float) -> Audio:
    t = np.arange(n) / SAMPLE_RATE
    out: Audio = amp * np.sin(2 * np.pi * freq * t)
    return out


def _chime(amp: float) -> Audio:
    n = _ms_to_n(900)
    t = np.arange(n) / SAMPLE_RATE
    decay = np.exp(-t * 5.0)
    first = np.sin(2 * np.pi * 988 * t) * decay
    second = np.where(t > 0.3, np.sin(2 * np.pi * 784 * (t - 0.3)) * np.exp(-(t - 0.3) * 5), 0)
    out: Audio = amp * (first + second)
    return out


def _siren(n: int, amp: float) -> Audio:
    t = np.arange(n) / SAMPLE_RATE
    freq = 950 + 400 * np.sin(2 * np.pi * 0.22 * t)
    phase = 2 * np.pi * np.cumsum(freq) / SAMPLE_RATE
    ramp = np.minimum(1.0, t / max(t[-1], 1e-9)) if n else t
    out: Audio = amp * ramp * np.sin(phase)
    return out


def _creak(rng: np.random.Generator, amp: float) -> Audio:
    n = _ms_to_n(rng.uniform(180, 320))
    t = np.arange(n) / SAMPLE_RATE
    freq = rng.uniform(260, 420) * (1 + 0.6 * t / t[-1])
    phase = 2 * np.pi * np.cumsum(freq) / SAMPLE_RATE
    grit = 0.5 + 0.5 * np.abs(rng.standard_normal(n))
    out: Audio = amp * np.sin(phase) * grit * np.sin(np.pi * t / t[-1])
    return out


def _add(track: Audio, clip: Audio, at: int) -> None:
    end = min(at + clip.size, track.size)
    if end > at >= 0:
        track[at:end] += clip[: end - at]


def mix(script: RecordingScript, speech: list[Audio], segments: list[Segment], n: int) -> Audio:
    rng = _rng(script)
    notice = np.zeros(n)
    voice = np.zeros(n)
    for turn, seg, clip in zip(script.turns, segments, speech, strict=True):
        spec = script.voices[turn.speaker]
        target = notice if turn.speaker == NOTICE_SPEAKER else voice
        _add(target, normalize(clip, spec.gain), _ms_to_n(seg.start_ms))
    # The background starts after the spoken synthetic notice.
    bg_start = _ms_to_n(segments[0].end_ms + 150)
    fade = np.clip((np.arange(n) - bg_start) / _ms_to_n(400), 0.0, 1.0)
    if script.background == "phone":
        street = 0.02 * _lowpass_noise(rng, n, 250.0)
        swell = 0.5 + 0.5 * np.sin(2 * np.pi * np.arange(n) / (SAMPLE_RATE * 9.0)) ** 2
        street += 0.012 * swell * _lowpass_noise(rng, n, 900.0)
        _add(street, _chime(0.06), _ms_to_n(segments[1].start_ms - 300))
        if script.siren_before_turn is not None:
            siren_at = _ms_to_n(max(segments[script.siren_before_turn].start_ms - 4000, 0))
            _add(street, _siren(n - siren_at, 0.05), siren_at)
        line = _bandlimit(voice + street * fade, 300.0, 3400.0)
        line = np.tanh(1.6 * line) / 1.6
        hiss = 0.002 * rng.standard_normal(n)
        out: Audio = notice + line + fade * (_tone(n, 120.0, 0.003) + hiss)
    else:
        room = 0.004 * _lowpass_noise(rng, n, 400.0) + _tone(n, 60.0, 0.0015)
        for prev, nxt in itertools.pairwise(segments[1:]):
            gap = nxt.start_ms - prev.end_ms
            if gap > 900 and rng.random() < 0.5:
                at = _ms_to_n(prev.end_ms + rng.uniform(0.25, 0.6) * gap)
                _add(room, _creak(rng, 0.03), at)
        out = notice + voice + room * fade
    peak = float(np.abs(out).max())
    return out * (0.95 / peak) if peak > 0.95 else out


def render(script: RecordingScript, synthesizer: Synthesizer) -> Rendered:
    speech = [synthesizer.synthesize(t.text, script.voices[t.speaker]) for t in script.turns]
    speech_ms = [round(c.size * 1000 / SAMPLE_RATE) for c in speech]
    segments, total_ms = layout(script, speech_ms)
    return Rendered(mix(script, speech, segments, _ms_to_n(total_ms)), segments)


def _info_chunk(fields: dict[bytes, str]) -> bytes:
    body = b"INFO"
    for key, value in fields.items():
        data = value.encode("ascii", "replace") + b"\x00"
        if len(data) % 2:
            data += b"\x00"
        body += key + struct.pack("<I", len(data)) + data
    return b"LIST" + struct.pack("<I", len(body)) + body


def write_wav(path: Path, audio: Audio, title: str) -> None:
    """16-bit PCM mono WAV with a LIST/INFO chunk carrying the SYNTHETIC notice."""
    pcm = np.clip(np.round(audio * 32767), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    info = _info_chunk({b"INAM": f"SYNTHETIC - {title}", b"ICMT": SYNTHETIC_NOTICE})
    data = path.read_bytes() + info
    path.write_bytes(data[:4] + struct.pack("<I", len(data) - 8) + data[8:])


def transcript(
    script: RecordingScript,
    rendered: Rendered,
    speaker_names: dict[str, str],
    recorded_at: str,
    synthesizer: str,
) -> dict[str, Any]:
    return {
        "notice": SYNTHETIC_NOTICE,
        "evidence_id": script.evidence_id,
        "title": script.title,
        "recorded_at": recorded_at,
        "duration_ms": rendered.duration_ms,
        "sample_rate": SAMPLE_RATE,
        "synthesizer": synthesizer,
        "speakers": [
            {"id": sid, "name": speaker_names.get(sid, sid), "voice": spec.voice}
            for sid, spec in script.voices.items()
        ],
        "segments": [
            {"start_ms": s.start_ms, "end_ms": s.end_ms, "speaker": s.speaker, "text": s.text}
            for s in rendered.segments
        ],
    }


def write_transcript(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
