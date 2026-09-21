"""Check rf-agent messages against the version 1 contract.

    uv run --with jsonschema python tools/validate.py examples/*.json

Each file holds one message, a Batch or a Status. The schema is
`schema/rf-message-v1.schema.json`, the same file sonoran checks every
message against, so a message that passes here is one sonoran accepts.
It prints one line per file and exits 1 if any fails.
"""

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "rf-message-v1.schema.json"


def main(paths: list[str]) -> int:
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()), format_checker=FormatChecker())
    failed = 0
    for path in paths:
        errors = sorted(validator.iter_errors(json.loads(Path(path).read_text())), key=str)
        if errors:
            failed += 1
            print(f"FAIL {path}: {errors[0].message}")
        else:
            print(f"ok   {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
