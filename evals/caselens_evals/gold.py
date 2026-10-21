"""The eval gold set: a strict model of one gold question, its loader, and the shared citation
matching rule.

Gold set file: `evals/gold/<story>.jsonl`, one `GoldItem` per line, sorted by id. It is built by
`python -m caselens_evals.build_gold` from the SYNTHETIC ground-truth story, the answer key and
the manifest; never hand-edit it.

Citation matching rule (shared by every eval ticket): a run citation matches a required citation
when it is the same evidence file (compared by `original_filename`, i.e. the file's basename) and
the locators overlap:
  * pdf: same page, and the bboxes intersect (or either bbox is null);
  * audio: the [start_ms, end_ms] ranges overlap;
  * video: the [start_ms, end_ms] ranges overlap; a locator whose end_ms is null is a point at
    start_ms, and matches when it lies within +/- 2 s of the other locator's range;
  * record: same table and row_id.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["single_source", "multi_source", "structured", "conflict", "unanswerable"]
Status = Literal["answered", "insufficient_evidence"]
Modality = Literal["pdf", "audio", "video", "csv"]
Operation = Literal[
    "calls_between",
    "calls_for_phone",
    "call_count_between",
    "plate_sightings",
    "plate_sightings_near",
    "custody_chain",
]
RecordTable = Literal["call_records", "plate_reads", "custody_records"]

MIX: dict[str, int] = {
    "single_source": 15,
    "multi_source": 15,
    "structured": 10,
    "conflict": 5,
    "unanswerable": 5,
}
MODALITY_ORDER: tuple[Modality, ...] = ("pdf", "audio", "video", "csv")
VIDEO_POINT_TOLERANCE_MS = 2000

Bbox = Annotated[list[float], Field(min_length=4, max_length=4)]


class GoldError(ValueError):
    """The gold set breaks one of its set-level rules (mix, ids)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PdfLocator(_Strict):
    kind: Literal["pdf"]
    page: int = Field(ge=1)
    bbox: Bbox | None = None


class AudioLocator(_Strict):
    kind: Literal["audio"]
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker: str | None = None


class VideoLocator(_Strict):
    kind: Literal["video"]
    start_ms: int = Field(ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    frame_idx: int | None = Field(default=None, ge=0)
    bbox: Bbox | None = None
    track_id: str | None = None


class RecordLocator(_Strict):
    kind: Literal["record"]
    table: RecordTable
    row_id: int = Field(ge=1)


Locator = Annotated[
    PdfLocator | AudioLocator | VideoLocator | RecordLocator, Field(discriminator="kind")
]

_KIND_MODALITY: dict[str, Modality] = {
    "pdf": "pdf",
    "audio": "audio",
    "video": "video",
    "record": "csv",
}


class RequiredCitation(_Strict):
    fact_id: str = Field(min_length=1)
    evidence: str = Field(pattern=r"^(pdf|audio|video|csv)/[^/]+$")
    locator: Locator

    @property
    def filename(self) -> str:
        return PurePosixPath(self.evidence).name

    @property
    def modality(self) -> Modality:
        return _KIND_MODALITY[self.locator.kind]

    @model_validator(mode="after")
    def _folder_matches_kind(self) -> RequiredCitation:
        folder = self.evidence.split("/", 1)[0]
        if folder != self.modality:
            raise ValueError(f"{self.evidence}: a {self.locator.kind} locator needs {folder}/")
        return self

    def locator_dict(self) -> dict[str, Any]:
        """The locator as the answer key writes it (null fields left out)."""
        return {k: v for k, v in self.locator.model_dump(mode="json").items() if v is not None}


class ConflictSpec(_Strict):
    id: str = Field(pattern=r"^cf_\d{2}$")
    sides: list[str] = Field(min_length=2, max_length=2)


class SqlExpected(_Strict):
    row_count: int | None = Field(default=None, ge=0)
    rows_match: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _something_expected(self) -> SqlExpected:
        if self.row_count is None and self.rows_match is None:
            raise ValueError("sql.expected needs row_count or rows_match")
        return self


class SqlSpec(_Strict):
    operation: Operation
    params: dict[str, str]
    expected: SqlExpected


class GoldItem(_Strict):
    id: str = Field(pattern=r"^q\d{2}$")
    category: Category
    question: str = Field(min_length=1)
    expected_status: Status
    expected_answer: str = Field(min_length=1)
    key_facts: list[str] = Field(min_length=1)
    required_citations: list[RequiredCitation]
    conflict: ConflictSpec | None
    sql: SqlSpec | None
    modalities: list[Modality]
    source_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> GoldItem:
        unanswerable = self.category == "unanswerable"
        want_status = "insufficient_evidence" if unanswerable else "answered"
        if self.expected_status != want_status:
            raise ValueError(f"{self.id}: {self.category} needs expected_status {want_status}")
        if unanswerable and self.required_citations:
            raise ValueError(f"{self.id}: unanswerable items cite nothing")
        if not unanswerable and not self.required_citations:
            raise ValueError(f"{self.id}: required_citations must not be empty")
        if (self.conflict is not None) != (self.category == "conflict"):
            raise ValueError(f"{self.id}: conflict is set exactly for the conflict category")
        if (self.sql is not None) != (self.category == "structured"):
            raise ValueError(f"{self.id}: sql is set exactly for the structured category")
        cited = {c.modality for c in self.required_citations}
        want = [m for m in MODALITY_ORDER if m in cited]
        if self.modalities != want:
            raise ValueError(f"{self.id}: modalities must be {want} (from the citations)")
        keys = [(c.fact_id, c.evidence, repr(c.locator)) for c in self.required_citations]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{self.id}: duplicate required citation")
        return self

    def to_json_dict(self) -> dict[str, Any]:
        """JSON form for the JSONL file (locators without null fields, like the answer key)."""
        data = self.model_dump(mode="json")
        data["required_citations"] = [
            {"fact_id": c.fact_id, "evidence": c.evidence, "locator": c.locator_dict()}
            for c in self.required_citations
        ]
        return data


def check_mix(items: Iterable[GoldItem]) -> None:
    """Raise `GoldError` unless the set is exactly q01..q50 with the required category mix."""
    items = list(items)
    ids = [i.id for i in items]
    if len(ids) != len(set(ids)):
        raise GoldError("duplicate gold ids")
    total = sum(MIX.values())
    want_ids = {f"q{n:02d}" for n in range(1, total + 1)}
    if set(ids) != want_ids:
        raise GoldError(f"gold ids must be exactly q01..q{total:02d}")
    counts = Counter(i.category for i in items)
    if dict(counts) != MIX:
        raise GoldError(f"category mix {dict(counts)} != {MIX}")


def load_gold(path: str | Path, *, check: bool = True) -> list[GoldItem]:
    """Load a gold JSONL file, validating every line (and the set-level mix when `check`)."""
    items: list[GoldItem] = []
    text = Path(path).read_text(encoding="utf-8")
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            items.append(GoldItem.model_validate_json(line))
        except ValueError as exc:
            raise GoldError(f"{path}:{n}: {exc}") from exc
    if len({i.id for i in items}) != len(items):
        raise GoldError(f"{path}: duplicate gold ids")
    if check:
        check_mix(items)
    return items


def _ranges_overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 <= b1 and b0 <= a1


def _bbox_overlap(a: Any, b: Any) -> bool:
    if not a or not b:
        return True
    return bool(a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3])


def _video_overlap(run: Mapping[str, Any], want: VideoLocator) -> bool:
    r0 = int(run["start_ms"])
    r_end = run.get("end_ms")
    w0, w_end = want.start_ms, want.end_ms
    if r_end is not None and w_end is not None:
        return _ranges_overlap(r0, int(r_end), w0, w_end)
    tol = VIDEO_POINT_TOLERANCE_MS
    if r_end is None and w_end is None:
        return abs(r0 - w0) <= tol
    if w_end is not None:  # the run locator is a point
        return w0 - tol <= r0 <= w_end + tol
    return r0 - tol <= w0 <= int(str(r_end)) + tol


def match_citation(
    run_citation_filename: str, run_locator: Mapping[str, Any], required: RequiredCitation
) -> bool:
    """True when a run citation (its evidence's `original_filename` and its locator) matches a
    required gold citation under the shared rule in this module's docstring."""
    name = PurePosixPath(run_citation_filename.replace("\\", "/")).name
    if name != required.filename:
        return False
    want = required.locator
    if run_locator.get("kind") != want.kind:
        return False
    try:
        if isinstance(want, PdfLocator):
            return int(run_locator["page"]) == want.page and _bbox_overlap(
                run_locator.get("bbox"), want.bbox
            )
        if isinstance(want, AudioLocator):
            return _ranges_overlap(
                int(run_locator["start_ms"]), int(run_locator["end_ms"]), want.start_ms, want.end_ms
            )
        if isinstance(want, VideoLocator):
            return _video_overlap(run_locator, want)
        return bool(
            run_locator["table"] == want.table and int(run_locator["row_id"]) == want.row_id
        )
    except (KeyError, TypeError, ValueError):
        return False


__all__ = [
    "MIX",
    "AudioLocator",
    "ConflictSpec",
    "GoldError",
    "GoldItem",
    "PdfLocator",
    "RecordLocator",
    "RequiredCitation",
    "SqlExpected",
    "SqlSpec",
    "VideoLocator",
    "check_mix",
    "load_gold",
    "match_citation",
]
