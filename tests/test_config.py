from __future__ import annotations

from pathlib import Path

import pytest

from homelab_mcp.config import load_config, load_config_text
from homelab_mcp.errors import ConfigError

_MINIMAL = """
[server]
transport = "stdio"
cache_ttl_seconds = 20

[adapters.netbox]
enabled = true
base_url = "https://netbox.example.invalid"
token_env = "NETBOX_TOKEN"

[adapters.proxmox]
enabled = false
base_url = "https://pve.example.invalid:8006"
token_id_env = "PVE_ID"
token_secret_env = "PVE_SECRET"
"""


def test_parses_server_and_adapters() -> None:
    cfg = load_config_text(_MINIMAL)
    assert cfg.server.transport == "stdio"
    assert cfg.server.cache_ttl_seconds == 20
    assert {a.name for a in cfg.enabled_adapters()} == {"netbox"}


def test_unknown_transport_rejected() -> None:
    with pytest.raises(ConfigError, match="transport"):
        load_config_text('[server]\ntransport = "carrier-pigeon"\n')


def test_http_fields_default_and_parse() -> None:
    default = load_config_text('[server]\ntransport = "stdio"\n')
    assert (default.server.host, default.server.port, default.server.http_path) == (
        "127.0.0.1",
        8000,
        "/mcp",
    )
    custom = load_config_text(
        '[server]\ntransport = "streamable-http"\n'
        'host = "192.0.2.10"\nport = 9000\nhttp_path = "/homelab"\n'
    )
    assert custom.server.host == "192.0.2.10"
    assert custom.server.port == 9000
    assert custom.server.http_path == "/homelab"


@pytest.mark.parametrize(
    "line",
    [
        "port = 0",
        "port = 70000",
        "port = true",
        'http_path = "mcp"',  # missing leading slash
        'host = ""',
        'log_level = "CHATTY"',
    ],
)
def test_bad_server_fields_rejected(line: str) -> None:
    with pytest.raises(ConfigError):
        load_config_text(f'[server]\ntransport = "streamable-http"\n{line}\n')


def test_log_level_normalised() -> None:
    cfg = load_config_text('[server]\nlog_level = "debug"\n')
    assert cfg.server.log_level == "DEBUG"


def test_unknown_top_level_key_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown top-level"):
        load_config_text("[bogus]\nx = 1\n")


def test_adapter_without_enabled_rejected() -> None:
    with pytest.raises(ConfigError, match="enabled"):
        load_config_text('[adapters.netbox]\nbase_url = "x"\n')


def test_negative_cache_ttl_rejected() -> None:
    with pytest.raises(ConfigError, match="cache_ttl_seconds"):
        load_config_text("[server]\ncache_ttl_seconds = -1\n")


def test_resolve_secrets_reports_all_missing() -> None:
    cfg = load_config_text(_MINIMAL)
    with pytest.raises(ConfigError) as exc:
        cfg.resolve_secrets(environ={})
    assert "netbox.token_env -> $NETBOX_TOKEN" in str(exc.value)
    # proxmox is disabled, so its missing vars must not be reported
    assert "PVE_ID" not in str(exc.value)


def test_resolve_secrets_success() -> None:
    cfg = load_config_text(_MINIMAL)
    resolved = cfg.resolve_secrets(environ={"NETBOX_TOKEN": "s3cr3t"})
    assert resolved["netbox"] == {"NETBOX_TOKEN": "s3cr3t"}


def test_resolve_secrets_finds_nested_env_fields() -> None:
    cfg = load_config_text(
        """
[adapters.http_json]
enabled = true

  [adapters.http_json.endpoints.one]
  url = "https://a.example.invalid"
  token_env = "ONE_TOKEN"

  [adapters.http_json.endpoints.two]
  url = "https://b.example.invalid"
  token_env = "TWO_TOKEN"
"""
    )
    with pytest.raises(ConfigError) as exc:
        cfg.resolve_secrets(environ={"ONE_TOKEN": "x"})
    assert "TWO_TOKEN" in str(exc.value)
    assert "ONE_TOKEN" not in str(exc.value)

    resolved = cfg.resolve_secrets(environ={"ONE_TOKEN": "x", "TWO_TOKEN": "y"})
    assert resolved["http_json"] == {"ONE_TOKEN": "x", "TWO_TOKEN": "y"}


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_load_config_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "c.toml"
    path.write_text(_MINIMAL, encoding="utf-8")
    monkeypatch.setenv("HOMELAB_MCP_CONFIG", str(path))
    cfg = load_config()
    assert cfg.server.cache_ttl_seconds == 20


def test_example_config_is_valid() -> None:
    example = Path(__file__).resolve().parents[1] / "homelab-mcp.example.toml"
    cfg = load_config_text(example.read_text(encoding="utf-8"))
    # every adapter in the example ships disabled
    assert cfg.enabled_adapters() == []
