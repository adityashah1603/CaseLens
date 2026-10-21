"""Assemble the Hugging Face Space repository folder (infra/space/build/, gitignored).

A Docker Space builds `Dockerfile` at the root of its own git repo, so this copies exactly the
build context of Dockerfile.space into one folder: the `!` allow-list entries of the root
`.dockerignore` (single source of truth), restricted to files git does not ignore (no .env,
node_modules, dist or caches), plus the exported demo evidence (infra/space/export_evidence.py),
`Dockerfile.space` as `Dockerfile`, and the Space card `README.md`. Upload it with
`hf upload <user>/<space> infra/space/build . --repo-type space` (infra/space/README.md).

    .venv/Scripts/python infra/space/stage.py
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPACE = Path(__file__).resolve().parent
BUILD = SPACE / "build"
EVIDENCE = "infra/space/evidence/"
NEVER = (".env", ".env.*", "*.pem", "*.key", "*.pyc", "*.pyo")
NEVER_DIRS = ("__pycache__", "node_modules", "dist", ".cache")
CARD = """---
title: CaseLens
colorFrom: gray
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
short_description: Evidence-search copilot with cited answers (SYNTHETIC data)
---

# CaseLens demo (SYNTHETIC data)

An evidence-search copilot: ask a question about the fictional demo case "The Riverside Lot
Homicide" and get a cited timeline. Every person, place and file in this demo is SYNTHETIC.
Source and docs: https://github.com/adityashah1603/CaseLens
"""


def allow_list() -> list[str]:
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return [line[1:].strip() for line in lines if line.startswith("!")]


def candidates() -> list[str]:
    """Repo files git does not ignore (tracked, or new and not ignored), POSIX paths."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p and (ROOT / p).is_file()]


def allowed(path: str, patterns: list[str]) -> bool:
    parts = path.split("/")
    if any(part in NEVER_DIRS for part in parts[:-1]):
        return False
    if any(fnmatch.fnmatch(parts[-1], pattern) for pattern in NEVER):
        return False
    for pattern in patterns:
        if pattern.endswith("/") and path.startswith(pattern):
            return True
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def main() -> int:
    patterns = allow_list()
    evidence = ROOT / EVIDENCE
    if not (evidence / "manifest.json").is_file():
        raise SystemExit("run infra/space/export_evidence.py first (no evidence manifest)")
    files = [p for p in candidates() if allowed(p, patterns)]
    files += [
        p.relative_to(ROOT).as_posix()
        for p in sorted(evidence.rglob("*"))
        if p.is_file() and not p.name.startswith(".")
    ]
    if BUILD.exists():
        shutil.rmtree(BUILD)
    total = 0
    for rel in sorted(set(files)):
        target = BUILD / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
        total += target.stat().st_size
    shutil.copy2(ROOT / "Dockerfile.space", BUILD / "Dockerfile")
    shutil.copy2(ROOT / ".dockerignore", BUILD / ".dockerignore")
    # Inside the Space repo the Dockerfile is `Dockerfile`; keep the allow-list entry valid.
    ignore = (BUILD / ".dockerignore").read_text(encoding="utf-8")
    (BUILD / ".dockerignore").write_text(
        ignore.replace("!Dockerfile.space\n", "!Dockerfile.space\n!Dockerfile\n!README.md\n"),
        encoding="utf-8",
    )
    (BUILD / "README.md").write_text(CARD, encoding="utf-8")
    print(f"staged {len(set(files))} files ({total / 1e6:.1f} MB) in {BUILD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
