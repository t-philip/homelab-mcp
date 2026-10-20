#!/usr/bin/env python3
"""Pin every vendored module to a known SHA-256.

``secret_redaction.py`` and ``credentials.py`` are each carried byte-identical by
more than one project. They are separate repos, so a module can't be a shared
import.

This check runs standalone in one repo: it recomputes each module's hash and
fails if one changed without its pin being updated. The pins below are the
**same values** in every vendoring project's copy of this script; a reviewer
seeing the pins diverge knows the copies drifted. To change a module: edit it in
every project, run ``python scripts/check_vendored.py --update`` in each, commit.

``scripts/check_vendored.py`` is itself vendored byte-identical.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

EXPECTED_SHA256 = {
    "secret_redaction.py": "54adaa0a6257e3cf3b057a8e5a91b46dda2869e083336506bda04207eee165b6",
    "credentials.py": "96bf39a72e8b5f4435052d0d392bf8492fb68ccdecc15709fb6d921b994f70e9",
}

_SRC = Path(__file__).resolve().parent.parent / "src"


def _module_path(name: str) -> Path:
    hits = sorted(_SRC.glob(f"*/{name}"))
    if len(hits) != 1:
        print(f"expected exactly one src/*/{name}, found {hits}", file=sys.stderr)
        raise SystemExit(2)
    return hits[0]


def main(argv: list[str]) -> int:
    update = "--update" in argv
    me = Path(__file__)
    text = me.read_text(encoding="utf-8")
    rc = 0
    for name, pinned in EXPECTED_SHA256.items():
        path = _module_path(name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if update:
            text = re.sub(rf'("{re.escape(name)}": )"[0-9a-f]*"', rf'\1"{digest}"', text, count=1)
            print(f"pinned {name} -> {digest}")
        elif digest != pinned:
            print(
                f"DRIFT: src/*/{name} sha256 is {digest}\n"
                f"       expected {pinned or '(unset)'}\n"
                f"       edit the module in BOTH packages, then run "
                f"`python scripts/check_vendored.py --update` in both.",
                file=sys.stderr,
            )
            rc = 1
        else:
            print(f"vendored {name}: sha256 {digest[:16]}… matches pin")
    if update:
        me.write_text(text, encoding="utf-8")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
