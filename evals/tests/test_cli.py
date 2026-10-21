from pathlib import Path

import pytest

from caselens_evals.__main__ import main


def test_bare_command_is_the_smoke_eval_and_skips_without_gold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CASELENS_EVAL_REQUIRE_LIVE", raising=False)
    monkeypatch.setattr("caselens_evals.cli.GOLD_PATH", tmp_path / "missing.jsonl")
    assert main([]) == 0
