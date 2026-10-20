from __future__ import annotations

from pathlib import Path

import pytest

from homelab_mcp.cli import _load, main

_NO_ADAPTERS = """
[server]
transport = "stdio"

[adapters.systemd]
enabled = false
hosts = ["node-a"]
"""


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "homelab-mcp" in capsys.readouterr().out


def test_run_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_doctor_missing_config_is_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["doctor", "-c", "does-not-exist.toml"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_doctor_with_no_enabled_adapters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(_NO_ADAPTERS, encoding="utf-8")
    rc = main(["doctor", "-c", str(cfg)])
    assert rc == 1
    assert "no adapters" in capsys.readouterr().out


def test_transport_override_rejects_bad_value() -> None:
    with pytest.raises(SystemExit):
        main(["run", "-c", "x.toml", "--transport", "smoke-signals"])


def test_doctor_reports_per_adapter_secret_gap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # netbox is enabled but NETBOX_TOKEN is unset. doctor must still run and show
    # a FAIL row for netbox rather than aborting the whole command.
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        "[adapters.netbox]\nenabled = true\n"
        'base_url = "https://n.example.invalid"\ntoken_env = "NETBOX_TOKEN_UNSET_XYZ"\n'
        '[adapters.systemd]\nenabled = false\nhosts = ["node-a"]\n',
        encoding="utf-8",
    )
    rc = main(["doctor", "-c", str(cfg)])
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "netbox" in out
    assert "NETBOX_TOKEN_UNSET_XYZ" in out
    assert rc == 1


def test_cli_overrides_apply_to_server_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text(_NO_ADAPTERS, encoding="utf-8")

    unchanged = _load(str(cfg_path))
    assert unchanged.server.transport == "stdio"
    assert unchanged.server.port == 8000

    overridden = _load(str(cfg_path), transport="streamable-http", host="198.51.100.10", port=9999)
    assert overridden.server.transport == "streamable-http"
    assert overridden.server.host == "198.51.100.10"
    assert overridden.server.port == 9999
    # untouched fields carry through
    assert overridden.server.http_path == "/mcp"
