"""File-first credential source — resolve a secret name to its value without the
value ever entering ``os.environ`` / ``/proc/<pid>/environ``.

A value in the process environment is readable through ``/proc/<pid>/environ``,
``ps eww``, a core dump, and is inherited by every child process. systemd can
instead hand a unit its secrets as files under ``$CREDENTIALS_DIRECTORY``
(``LoadCredential=`` / ``ImportCredential=`` — a 0400 ``tmpfs``, service-only,
removed on stop). Only code that *reads those files* benefits; this is that
reader.

``CredentialSource`` is a read-through :class:`~collections.abc.Mapping`:
``src["NAME"]``, ``src.get("NAME")`` and ``"NAME" in src`` each resolve by
trying, in order,

1. ``$CREDENTIALS_DIRECTORY/NAME``  — a systemd-supplied credential (or a
   directory a parent process points a co-located child at, holding only the
   child's allowed subset)
2. ``<extra_dir>/NAME``            — an explicit second directory
3. ``os.environ["NAME"]``          — fallback: tests and a plain-env deploy keep
   working unchanged

A file value is read once, has a single trailing newline stripped (a
``printf``-written creds file has none; an editor-saved one may), and is cached
for the process lifetime. A name that resolves nowhere is absent — exactly as
``os.environ`` would report it.

Swapping a call site over is a one-liner: ``environ or os.environ`` becomes
``environ if environ is not None else default_credential_source()`` — every
``.get`` / ``in`` / ``[]`` on the result behaves the same.

This module is kept byte-identical across the projects that vendor it;
``scripts/check_vendored.py`` pins its SHA-256 so a drift fails CI.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path

#: systemd sets this for a unit with any ``LoadCredential=`` / ``SetCredential=``
#: / ``ImportCredential=`` directive. A parent spawning a co-located child may
#: set it too, pointing at a directory holding only the child's allowed subset.
_SYSTEMD_CREDS_ENV = "CREDENTIALS_DIRECTORY"


def _read_file(path: Path) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    # A creds file written with `printf '%s'` has no trailing newline; one saved
    # from an editor has exactly one. Strip a single trailing "\n" only — never
    # internal whitespace, which is significant in a PEM key or a passphrase.
    return raw[:-1] if raw.endswith("\n") else raw


class CredentialSource(Mapping[str, str]):
    """Resolve credential names file-first, then from ``os.environ``.

    *extra_dir*, if given, is a second directory of ``NAME`` files searched
    after ``$CREDENTIALS_DIRECTORY`` and before ``os.environ``.
    """

    def __init__(self, *, extra_dir: str | os.PathLike[str] | None = None) -> None:
        self._dirs: list[Path] = []
        systemd_dir = os.environ.get(_SYSTEMD_CREDS_ENV)
        if systemd_dir:
            self._dirs.append(Path(systemd_dir))
        if extra_dir:
            self._dirs.append(Path(extra_dir))
        self._cache: dict[str, str | None] = {}

    def _resolve(self, key: str) -> str | None:
        if key in self._cache:
            return self._cache[key]
        value: str | None = None
        for directory in self._dirs:
            found = _read_file(directory / key)
            if found is not None:
                value = found
                break
        if value is None:
            value = os.environ.get(key)
        self._cache[key] = value
        return value

    def __getitem__(self, key: str) -> str:
        value = self._resolve(key)
        if value is None:
            raise KeyError(key)
        return value

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self._resolve(key) is not None

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for directory in self._dirs:
            try:
                names = sorted(p.name for p in directory.iterdir() if p.is_file())
            except OSError:
                names = []
            for name in names:
                if name not in seen:
                    seen.add(name)
                    yield name
        for name in os.environ:
            if name not in seen:
                seen.add(name)
                yield name

    def __len__(self) -> int:
        return sum(1 for _ in self)


_SOURCE: CredentialSource | None = None


def configure(*, extra_dir: str | os.PathLike[str] | None = None) -> CredentialSource:
    """Build the process-wide credential source and return it. Call once at
    startup — after argument parsing, before the first secret resolve."""
    global _SOURCE
    _SOURCE = CredentialSource(extra_dir=extra_dir)
    return _SOURCE


def default_credential_source() -> CredentialSource:
    """The process-wide credential source, built lazily with no *extra_dir* if
    :func:`configure` was never called."""
    global _SOURCE
    if _SOURCE is None:
        _SOURCE = CredentialSource()
    return _SOURCE


def _reset() -> None:
    """Test hook: drop the process-wide source so the next call rebuilds it."""
    global _SOURCE
    _SOURCE = None
