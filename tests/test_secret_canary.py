"""Canary: a unique sentinel 'secret' must not survive to the log stream once
the real logging path (cli._configure_logging -> secret_redaction.install) is up
and the value is registered. A regression in the redaction wiring fails here in
CI — this test is the sink tripwire.
"""

from __future__ import annotations

import io
import logging
import uuid
from collections.abc import Iterator

import pytest

from homelab_mcp import secret_redaction
from homelab_mcp.cli import _configure_logging


@pytest.fixture
def sentinel_log() -> Iterator[tuple[str, io.StringIO]]:
    """Real logging setup + an attached buffer + a fresh registered sentinel."""
    token = f"cnry-{uuid.uuid4().hex}-tail"
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        _configure_logging("INFO")  # installs the filter on root + handlers
        secret_redaction.install()  # (re)attach to the buffer handler too
        secret_redaction.redact_secrets([token])
        yield token, buf
    finally:
        root.removeHandler(handler)


def test_sentinel_not_in_httpx_style_info_line(sentinel_log: tuple[str, io.StringIO]) -> None:
    token, buf = sentinel_log
    logging.getLogger("httpx").warning(
        "HTTP Request: POST https://api.telegram.org/bot%s/getMe", token
    )
    out = buf.getvalue()
    assert token not in out
    assert "«redacted»" in out


def test_sentinel_not_in_exception_text(sentinel_log: tuple[str, io.StringIO]) -> None:
    token, buf = sentinel_log
    try:
        raise RuntimeError(f"auth failed for token {token}")
    except RuntimeError:
        logging.getLogger("homelab_mcp.test").error("call failed", exc_info=True)
    assert token not in buf.getvalue()


def test_sentinel_not_in_repr_of_secretstr(sentinel_log: tuple[str, io.StringIO]) -> None:
    token, buf = sentinel_log
    logging.getLogger("homelab_mcp.test").info("value is %r", secret_redaction.SecretStr(token))
    # repr masks it even before the value-filter runs
    assert token not in buf.getvalue()
