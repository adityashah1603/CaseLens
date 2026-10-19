"""Generate the SYNTHETIC evidence for one or all story cases.

    python -m caselens_data_gen                         # every case, every generator
    python -m caselens_data_gen --case harbor_street    # one case
    python -m caselens_data_gen --only structured,documents

Writes data_gen/output/<case>/ plus manifest.json and answer_key.json per case.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Protocol

from caselens_data_gen.common import SYNTHETIC_NOTICE, GeneratedFile, write_manifest
from caselens_data_gen.story import STORY_SLUGS, Story, load_story

GENERATORS = ("structured", "documents", "audio", "video")
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "output"


class _Generator(Protocol):
    def __call__(self, story: Story, case_dir: Path) -> list[GeneratedFile]: ...


def _load_generator(name: str) -> _Generator:
    module = importlib.import_module(f"caselens_data_gen.{name}")
    generate: _Generator = module.generate
    return generate


def run(slugs: list[str], generators: list[str], out_dir: Path) -> int:
    print(SYNTHETIC_NOTICE)
    for slug in slugs:
        story = load_story(slug)
        case_dir = out_dir / slug
        case_dir.mkdir(parents=True, exist_ok=True)
        files: list[GeneratedFile] = []
        for name in generators:
            produced = _load_generator(name)(story, case_dir)
            print(f"{slug}: {name} -> {len(produced)} file(s)")
            files.extend(produced)
        write_manifest(case_dir, files)
        print(f"{slug}: {len(files)} file(s), manifest + answer key in {case_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="caselens_data_gen", description=__doc__)
    parser.add_argument("--case", choices=STORY_SLUGS, action="append", dest="cases")
    parser.add_argument("--only", help=f"comma-separated subset of: {', '.join(GENERATORS)}")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    generators = list(GENERATORS)
    if args.only:
        generators = [g.strip() for g in args.only.split(",") if g.strip()]
        unknown = sorted(set(generators) - set(GENERATORS))
        if unknown:
            parser.error(f"unknown generator(s): {', '.join(unknown)}")
    return run(list(args.cases or STORY_SLUGS), generators, args.out)


if __name__ == "__main__":
    sys.exit(main())
