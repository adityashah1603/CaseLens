"""SYNTHETIC structured evidence (CSV) for one story case.

Writes ``<case_dir>/csv/`` with three files whose columns map 1:1 onto the DB contract
(``call_records``, ``plate_reads``, ``custody_records`` minus id/case_id/evidence_id):

* ``call_detail_records.csv``  story calls among deterministic noise calls
* ``plate_reader_hits.csv``    story ALPR reads among deterministic noise reads
* ``evidence_log.csv``         one row per story evidence item

File format: line 1 is a comment, ``# `` + ``SYNTHETIC_NOTICE``; loaders skip every line that
starts with ``#``. Line 2 is the header; ``row_id`` (1-based data row number) is column 1.
Timestamps are ISO 8601 with a UTC offset. Rows are chronological (evidence log: by item id).
Output is byte-identical for the same story (seeded ``random.Random`` per case and table).
"""

from __future__ import annotations

from pathlib import Path

from caselens_data_gen.common import GeneratedFile
from caselens_data_gen.story import Story
from caselens_data_gen.structured import calls, custody, plates

SUBFOLDER = "csv"


def generate(story: Story, case_dir: Path) -> list[GeneratedFile]:
    csv_dir = case_dir / SUBFOLDER
    csv_dir.mkdir(parents=True, exist_ok=True)
    return [
        calls.generate(story, csv_dir),
        plates.generate(story, csv_dir),
        custody.generate(story, csv_dir),
    ]


__all__ = ["SUBFOLDER", "generate"]
