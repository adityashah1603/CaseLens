"""Turn diarized words into speaker-turn chunks with audio locators."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from caselens_workers.audio_asr import Word
from caselens_workers.chunks import AudioLocator, ChunkIn

SENTENCE_END = (".", "?", "!")


@dataclass(frozen=True)
class Turn:
    speaker: str
    words: tuple[Word, ...]

    @property
    def start_ms(self) -> int:
        return self.words[0].start_ms

    @property
    def end_ms(self) -> int:
        return max(w.end_ms for w in self.words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def build_turns(words: Sequence[Word], speakers: Sequence[str]) -> list[Turn]:
    """Merge consecutive same-speaker words into turns."""
    if len(words) != len(speakers):
        raise ValueError(f"{len(words)} words but {len(speakers)} speaker labels")
    turns: list[Turn] = []
    cur: list[Word] = []
    cur_spk = ""
    for w, spk in zip(words, speakers, strict=True):
        if cur and spk != cur_spk:
            turns.append(Turn(cur_spk, tuple(cur)))
            cur = []
        cur.append(w)
        cur_spk = spk
    if cur:
        turns.append(Turn(cur_spk, tuple(cur)))
    return turns


def split_turn(turn: Turn, max_ms: int, max_words: int) -> list[Turn]:
    """Split a turn longer than `max_ms` or `max_words`, preferring the last sentence end."""
    pieces: list[Turn] = []
    cur: list[Word] = []
    for w in turn.words:
        while cur and (w.end_ms - cur[0].start_ms > max_ms or len(cur) + 1 > max_words):
            cut = next(
                (i for i in range(len(cur) - 1, -1, -1) if cur[i].text.endswith(SENTENCE_END)),
                len(cur) - 1,
            )
            pieces.append(Turn(turn.speaker, tuple(cur[: cut + 1])))
            cur = cur[cut + 1 :]
        cur.append(w)
    if cur:
        pieces.append(Turn(turn.speaker, tuple(cur)))
    return pieces


def turns_to_chunks(
    turns: Sequence[Turn],
    recorded_at: datetime | None,
    max_ms: int,
    max_words: int,
) -> list[ChunkIn]:
    """One chunk per (split) turn, text prefixed with the speaker label. Times are wall-clock
    when the recording start is known."""
    chunks: list[ChunkIn] = []
    for turn in turns:
        for piece in split_turn(turn, max_ms, max_words):
            start, end = piece.start_ms, piece.end_ms
            chunks.append(
                ChunkIn(
                    chunk_index=len(chunks),
                    text=f"{piece.speaker}: {piece.text}",
                    locator=AudioLocator(start_ms=start, end_ms=end, speaker=piece.speaker),
                    t_start=None
                    if recorded_at is None
                    else recorded_at + timedelta(milliseconds=start),
                    t_end=None
                    if recorded_at is None
                    else recorded_at + timedelta(milliseconds=end),
                )
            )
    return chunks
