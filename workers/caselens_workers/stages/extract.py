"""Shared `extract` stage (entities, events, timestamps from text-bearing chunks).

Placeholder pass-through so pipelines can chain to it; replaced by the extraction ticket.
"""

from __future__ import annotations

from caselens_workers.context import StageContext, StageResult


def extract(ctx: StageContext) -> StageResult:
    return StageResult(summary={"extracted": 0})
