"""CredentialSource: file-first resolution, os.environ fallback, and the
process-wide singleton. The point of the module is that a secret can be read
without ever being in ``os.environ`` — these tests pin that contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from homelab_mcp import credentials
from homelab_mcp.credentials import CredentialSource


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    credentials._reset()
    yield
    credentials._reset()


def _write(directory: Path, name: str, value: str) -> None:
    (directory / name).write_text(value, encoding="utf-8")


def test_reads_from_systemd_credentials_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "API_TOKEN", "from-file")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.delenv("API_TOKEN", raising=False)

    src = CredentialSource()

    assert src["API_TOKEN"] == "from-file"
    assert src.get("API_TOKEN") == "from-file"
    assert "API_TOKEN" in src


def test_file_wins_over_environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, "API_TOKEN", "from-file")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("API_TOKEN", "from-env")

    assert CredentialSource()["API_TOKEN"] == "from-file"


def test_falls_back_to_environ_when_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("API_TOKEN", "from-env")

    assert CredentialSource()["API_TOKEN"] == "from-env"


def test_extra_dir_searched_after_credentials_dir_before_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sysd = tmp_path / "sysd"
    extra = tmp_path / "extra"
    sysd.mkdir()
    extra.mkdir()
    _write(extra, "ONLY_IN_EXTRA", "extra-value")
    _write(sysd, "IN_BOTH", "sysd-value")
    _write(extra, "IN_BOTH", "extra-value")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(sysd))
    monkeypatch.setenv("ONLY_IN_EXTRA", "env-value")

    src = CredentialSource(extra_dir=extra)

    assert src["IN_BOTH"] == "sysd-value"  # $CREDENTIALS_DIRECTORY first
    assert src["ONLY_IN_EXTRA"] == "extra-value"  # extra dir beats os.environ


def test_single_trailing_newline_is_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "PRINTF_STYLE", "no-newline")
    _write(tmp_path, "EDITOR_STYLE", "one-newline\n")
    _write(tmp_path, "PEM_STYLE", "-----BEGIN-----\nbody line\n-----END-----\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))

    src = CredentialSource()

    assert src["PRINTF_STYLE"] == "no-newline"
    assert src["EDITOR_STYLE"] == "one-newline"
    # internal newlines survive; only one trailing "\n" is removed
    assert src["PEM_STYLE"] == "-----BEGIN-----\nbody line\n-----END-----"


def test_missing_key_behaves_like_absent_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.delenv("NOPE", raising=False)

    src = CredentialSource()

    assert "NOPE" not in src
    assert src.get("NOPE") is None
    assert src.get("NOPE", "") == ""
    with pytest.raises(KeyError):
        _ = src["NOPE"]


def test_value_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "ROTATING"
    path.write_text("v1", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))

    src = CredentialSource()
    assert src["ROTATING"] == "v1"
    path.write_text("v2", encoding="utf-8")
    assert src["ROTATING"] == "v1"  # read once, held for the process lifetime


def test_mapping_iteration_unions_files_and_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "FILE_ONLY", "x")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("ENV_ONLY", "y")

    src = CredentialSource()
    keys = set(src)

    assert {"FILE_ONLY", "ENV_ONLY"} <= keys
    assert len(src) == len(keys)


def test_configure_and_default_singleton(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, "SCOPED", "scoped-value")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("SCOPED", raising=False)

    configured = credentials.configure(extra_dir=tmp_path)
    assert credentials.default_credential_source() is configured
    assert credentials.default_credential_source()["SCOPED"] == "scoped-value"

    credentials._reset()
    assert credentials.default_credential_source() is not configured
