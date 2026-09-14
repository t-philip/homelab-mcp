"""Tests for the vendored secret-redaction filter."""

from __future__ import annotations

import logging

import pytest

from homelab_mcp.secret_redaction import (
    PLACEHOLDER,
    SecretRedactingFilter,
    SecretStr,
)


def test_scrub_replaces_registered_value() -> None:
    f = SecretRedactingFilter()
    f.register(["s3cr3t-token-value"])
    assert f.scrub("Authorization: Bearer s3cr3t-token-value here") == (
        f"Authorization: Bearer {PLACEHOLDER} here"
    )


def test_short_values_are_not_registered() -> None:
    f = SecretRedactingFilter()
    f.register(["abc", "12345"])  # both under MIN_LENGTH
    assert f.scrub("abc 12345") == "abc 12345"


def test_longest_needle_wins() -> None:
    f = SecretRedactingFilter()
    f.register(["tok_abcdef", "tok_abcdef_plus_more"])
    # The longer value fully redacts even though it contains the shorter one.
    assert f.scrub("x tok_abcdef_plus_more y") == f"x {PLACEHOLDER} y"


def test_multiline_value_also_scrubs_its_body_lines() -> None:
    pem = "-----BEGIN KEY-----\nAAAABBBBCCCCDDDD\nEEEEFFFFGGGGHHHH\n-----END KEY-----"
    f = SecretRedactingFilter()
    f.register([pem])
    # A stack trace that only surfaces one body line is still caught.
    assert f.scrub("ssh error near AAAABBBBCCCCDDDD") == f"ssh error near {PLACEHOLDER}"


def test_filter_redacts_formatted_message(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.redaction.formatted")
    f = SecretRedactingFilter()
    f.register(["hunter2-hunter2"])
    logger.addFilter(f)
    with caplog.at_level(logging.INFO, logger=logger.name):
        logger.info("connecting with password %s to host", "hunter2-hunter2")
    assert "hunter2-hunter2" not in caplog.text
    assert PLACEHOLDER in caplog.text


def test_filter_is_a_noop_before_any_registration() -> None:
    f = SecretRedactingFilter()
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "plain %s", ("arg",), None)
    assert f.filter(record) is True
    assert record.getMessage() == "plain arg"


def test_redacted_message_with_bare_percent_does_not_raise() -> None:
    f = SecretRedactingFilter()
    f.register(["leaky-secret-1"])
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "value leaky-secret-1 is 100%% done", (), None
    )
    f.filter(record)
    # args was cleared, so getMessage() must not re-run %-formatting.
    assert "leaky-secret-1" not in record.getMessage()
    assert "100%" in record.getMessage()


def test_exc_text_is_scrubbed() -> None:
    f = SecretRedactingFilter()
    f.register(["boom-secret-xyz"])
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), None)
    record.exc_text = "Traceback ... RuntimeError: token boom-secret-xyz rejected"
    f.filter(record)
    assert "boom-secret-xyz" not in (record.exc_text or "")
    assert PLACEHOLDER in (record.exc_text or "")


def test_secretstr_masks_repr_but_is_a_real_str() -> None:
    s = SecretStr("actual-value")
    assert s == "actual-value"
    assert str(s) == "actual-value"
    assert f"{s}" == "actual-value"
    assert repr(s) == "'«secret»'"
    assert "actual-value" not in repr({"k": s})
