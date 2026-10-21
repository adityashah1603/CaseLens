"""`python -m caselens_evals [run|score|report|smoke|full]`; no command = the smoke eval."""

import sys
from collections.abc import Sequence

from caselens_evals.cli import main as cli_main


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or (args[0].startswith("-") and args[0] not in ("-h", "--help")):
        args = ["smoke", *args]
    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
