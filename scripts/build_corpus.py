"""Generate synthetic PDF evidence outside the source repository."""

import argparse
import json
from pathlib import Path

from creditlens.corpus import build_corpus


def main() -> None:
    """Require a deliberate artifact path so thousands of PDF pages never enter the source tree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--borrowers", type=int, default=200)
    args = parser.parse_args()
    print(json.dumps(build_corpus(args.output, args.borrowers), indent=2))


if __name__ == "__main__":
    main()
