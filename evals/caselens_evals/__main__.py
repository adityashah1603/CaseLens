import sys
from pathlib import Path

GOLD_DIR = Path(__file__).resolve().parents[1] / "gold"


def main() -> int:
    gold_files = sorted(GOLD_DIR.glob("*.jsonl"))
    if not gold_files:
        print("caselens-evals: no gold set yet; smoke eval skipped (passes).")
        return 0
    print(
        f"caselens-evals: {len(gold_files)} gold file(s) found but no runner yet.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
