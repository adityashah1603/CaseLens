import json
from pathlib import Path

import pytest

from caselens_data_gen import __main__ as cli
from caselens_data_gen.common import FactLocation, GeneratedFile
from caselens_data_gen.story import Story


def _fake_generator(story: Story, case_dir: Path) -> list[GeneratedFile]:
    path = case_dir / "csv" / "fake.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# SYNTHETIC\nrow_id\n1\n", encoding="utf-8")
    fact = FactLocation(
        story.events[0].id, {"kind": "record", "table": "call_records", "row_id": 1}
    )
    return [GeneratedFile(path, "csv", "SYNTHETIC fake", [fact])]


def test_cli_writes_manifest_and_answer_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_load_generator", lambda name: _fake_generator)
    assert (
        cli.main(["--case", "harbor_street", "--only", "structured", "--out", str(tmp_path)]) == 0
    )

    case_dir = tmp_path / "harbor_street"
    manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
    answer_key = json.loads((case_dir / "answer_key.json").read_text(encoding="utf-8"))
    assert "SYNTHETIC" in manifest["notice"]
    assert [f["path"] for f in manifest["files"]] == ["csv/fake.csv"]
    assert len(manifest["files"][0]["sha256"]) == 64
    assert answer_key["facts"][0]["evidence"] == "csv/fake.csv"
    assert answer_key["facts"][0]["locator"]["row_id"] == 1


def test_cli_rejects_unknown_generator(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--only", "hologram", "--out", str(tmp_path)])
