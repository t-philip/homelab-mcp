#!/usr/bin/env python3
"""CI lint for the read-only guarantee.

1. Every declared tool is read-only by name, with no duplicates.
2. No adapter makes a non-GET HTTP call outside the explicit allowlist.

Exit 0 on success, 1 on any violation.
"""

from __future__ import annotations

import sys

from homelab_mcp._audit import find_non_get_http_calls
from homelab_mcp.adapters import iter_declared_tools
from homelab_mcp.naming import tool_name_violation


def main() -> int:
    violations: list[str] = []
    seen: set[str] = set()

    for spec in iter_declared_tools():
        reason = tool_name_violation(spec.name)
        if reason is not None:
            violations.append(reason)
        if spec.name in seen:
            violations.append(f"duplicate tool name {spec.name!r}")
        seen.add(spec.name)

    for filename, lineno, method, url in find_non_get_http_calls():
        violations.append(
            f"{filename}:{lineno} makes a {method} request to {url!r} "
            "(not GET, not on the read-only allowlist)"
        )

    if violations:
        print("FAIL: read-only guarantee")
        for item in violations:
            print(f"  - {item}")
        return 1

    print(f"ok: {len(seen)} declared tools read-only by name; no disallowed HTTP writes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
