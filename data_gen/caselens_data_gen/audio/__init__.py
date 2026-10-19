"""SYNTHETIC audio evidence: the 911 call and the recorded suspect interview.

Writes ``<case_dir>/audio/<evidence_id>.wav`` (16 kHz mono 16-bit PCM, spoken SYNTHETIC notice
first, LIST/INFO notice chunk) plus ``<evidence_id>.transcript.json``, the gold transcript with
per-segment times and speakers for the ASR (WER) and diarization evals. The transcript sidecar is
not evidence and is not listed in the manifest.
"""

from __future__ import annotations

from pathlib import Path

from caselens_data_gen.audio.render import (
    Rendered,
    render,
    transcript,
    write_transcript,
    write_wav,
)
from caselens_data_gen.audio.script import NOTICE_SPEAKER, RecordingScript, build_scripts
from caselens_data_gen.audio.synth import PiperSynthesizer, Synthesizer, ToneSynthesizer
from caselens_data_gen.common import FactLocation, GeneratedFile
from caselens_data_gen.story import Story

__all__ = [
    "PiperSynthesizer",
    "Synthesizer",
    "ToneSynthesizer",
    "build_scripts",
    "fact_locations",
    "generate",
]


def fact_locations(rendered: Rendered, names: dict[str, str]) -> list[FactLocation]:
    facts: list[FactLocation] = []
    for seg in rendered.segments:
        for fact_id in seg.facts:
            locator = {
                "kind": "audio",
                "start_ms": seg.start_ms,
                "end_ms": seg.end_ms,
                "speaker": seg.speaker,
            }
            note = f'{names.get(seg.speaker, seg.speaker)}: "{seg.text}"'
            facts.append(FactLocation(fact_id=fact_id, locator=locator, note=note))
    return facts


def _speaker_names(story: Story, script: RecordingScript) -> dict[str, str]:
    names = {p.id: f"{p.title} {p.name}" if p.title else p.name for p in story.people}
    names[NOTICE_SPEAKER] = "Synthetic notice (narrator voice)"
    return {sid: names.get(sid, sid) for sid in script.voices}


def generate(
    story: Story, case_dir: Path, synthesizer: Synthesizer | None = None
) -> list[GeneratedFile]:
    scripts = build_scripts(story)
    if not scripts:
        return []
    synth = synthesizer or PiperSynthesizer()
    out_dir = case_dir / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = {p.id: p for p in story.evidence_plan}
    produced: list[GeneratedFile] = []
    for script in scripts:
        recorded_at = plan[script.evidence_id].recorded_at.isoformat()
        names = _speaker_names(story, script)
        rendered = render(script, synth)
        wav_path = out_dir / f"{script.evidence_id}.wav"
        write_wav(wav_path, rendered.audio, script.title)
        write_transcript(
            out_dir / f"{script.evidence_id}.transcript.json",
            transcript(script, rendered, names, recorded_at, synth.name),
        )
        produced.append(
            GeneratedFile(
                path=wav_path,
                evidence_type="audio",
                description=f"SYNTHETIC {script.title}",
                facts=fact_locations(rendered, names),
                recorded_at=recorded_at,
            )
        )
    return produced
