"""Value-based log redaction — scrub known secret values out of every log record.

A secret store protects a value at rest and up to delivery; nothing models what
a consumer's *logging* then does with the plaintext. This is the general backstop
for that hop. It is a belt to a ``--log-level`` muzzle on known chatty
third-party loggers (some HTTP client libraries log the full request URL at
INFO, which can include a token embedded in a path or query string), not a
replacement: a logger nobody thought to muzzle would leak the same way, and this
catches it as long as the value was registered.

This module is kept byte-identical across the projects that vendor it;
``scripts/check_vendored.py`` pins its hash so a drift fails CI.

Usage::

    from . import secret_redaction

    secret_redaction.install()              # attach the filter (e.g. after logging setup)
    ...
    secret_redaction.redact_secrets(resolved.values())   # seed it once secrets are known
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

#: Replacement text. Chosen to be visually obvious and to contain no regex- or
#: printf-special characters.
PLACEHOLDER = "«redacted»"  # «redacted»

#: Values shorter than this are not registered — a 4-char token fragment is far
#: too collision-prone to blind-replace across every log line.
MIN_LENGTH = 6


class SecretRedactingFilter(logging.Filter):
    """Removes every registered secret substring from a :class:`logging.LogRecord`.

    Attach to **handlers**, not only to a logger: a filter on a logger never sees
    records that propagated up from a child logger — only that logger's own
    records. :func:`install` attaches to the root logger *and* its handlers.
    """

    def __init__(self) -> None:
        super().__init__()
        self._needles: list[str] = []

    def register(self, values: Iterable[object]) -> None:
        """Add *values* (and, for multi-line values, their non-trivial lines).

        Accepts ``object`` so a stray non-string can never make a *logging*
        filter raise; anything that is not a ``str`` is skipped.
        """
        for raw in values:
            if not isinstance(raw, str):
                continue
            for piece in _pieces(raw):
                if piece not in self._needles:
                    self._needles.append(piece)
        # Longest first: a value that contains a shorter registered value still
        # redacts fully in one pass.
        self._needles.sort(key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for needle in self._needles:
            if needle in text:
                text = text.replace(needle, PLACEHOLDER)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._needles:
            return True
        # getMessage() renders msg % args. If the caller passed a malformed
        # format that would raise here, it would raise at format time anyway
        # (the stdlib Formatter calls getMessage() unguarded too).
        message = record.getMessage()
        redacted = self.scrub(message)
        if redacted != message:
            # Replace with the fully-rendered, scrubbed text and drop args so
            # LogRecord.getMessage() returns it verbatim (no %-formatting when
            # args is falsy) — safe even if the text now contains a bare '%'.
            record.msg = redacted
            record.args = None
        if record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        return True


def _pieces(raw: str) -> list[str]:
    stripped = raw.strip()
    if len(stripped) < MIN_LENGTH:
        return []
    out = [stripped]
    if "\n" in stripped:
        # PEM keys and other multi-line secrets: a stack trace or an ssh error
        # may surface only the body lines.
        out.extend(
            line.strip() for line in stripped.splitlines() if len(line.strip()) >= MIN_LENGTH
        )
    return out


class SecretStr(str):
    """A ``str`` that masks itself in ``repr()`` — for ``%r``, tracebacks,
    ``pprint`` and dataclass ``repr``. It is a genuine ``str`` everywhere else
    (HTTP headers, ``str()`` in an f-string, passed to ``httpx``), so no call
    site needs to change."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "'«secret»'"


#: Process-wide singleton. One filter instance, shared by every handler.
_FILTER = SecretRedactingFilter()


def redact_secrets(values: Iterable[object]) -> None:
    """Register *values* with the process-wide redaction filter."""
    _FILTER.register(values)


def install(logger: logging.Logger | None = None) -> SecretRedactingFilter:
    """Attach the process-wide filter to *logger* (root by default) and to every
    handler it currently has. Idempotent — safe to call again after adding a
    handler."""
    target = logger if logger is not None else logging.getLogger()
    if _FILTER not in target.filters:
        target.addFilter(_FILTER)
    for handler in target.handlers:
        if _FILTER not in handler.filters:
            handler.addFilter(_FILTER)
    return _FILTER
