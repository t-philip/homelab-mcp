"""Configuration loading and validation.

The config file is TOML. Secrets are never stored in it: a field whose name
ends in ``_env`` holds the *name* of an environment variable, and the value is
read from the process environment at startup. An enabled adapter with a missing
endpoint or a missing secret env var is a fatal :class:`ConfigError` — the
server fails closed.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .credentials import default_credential_source
from .errors import ConfigError
from .secret_redaction import SecretStr

CONFIG_ENV_VAR = "HOMELAB_MCP_CONFIG"
VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
PUBLIC_BIND_ADDRESSES = frozenset({"0.0.0.0", "::", "[::]"})
_ALLOWED_TOP_LEVEL = frozenset({"server", "adapters"})
_SERVER_KEYS = frozenset(
    {
        "transport",
        "cache_ttl_seconds",
        "host",
        "port",
        "http_path",
        "log_level",
        "allowed_hosts",
        "allowed_origins",
    }
)


@dataclass(frozen=True)
class ServerConfig:
    transport: str = "stdio"
    cache_ttl_seconds: float = 15.0
    # Used only by the "sse" / "streamable-http" transports.
    host: str = "127.0.0.1"
    port: int = 8000
    http_path: str = "/mcp"
    log_level: str = "INFO"
    # HTTP Host / Origin headers the transport accepts (DNS-rebinding guard).
    # Empty -> a localhost + configured-host default is used.
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()

    def effective_allowed_hosts(self) -> list[str]:
        if self.allowed_hosts:
            return list(self.allowed_hosts)
        hosts = {self.host, "localhost", "127.0.0.1", "[::1]"}
        return sorted({*hosts, *(f"{h}:{self.port}" for h in hosts)})


@dataclass(frozen=True)
class AdapterConfig:
    name: str
    enabled: bool
    options: Mapping[str, Any]

    def secret_env_fields(self) -> dict[str, str]:
        """Map dotted field path -> env var name for every ``*_env`` option.

        Recurses into nested tables and arrays, so an adapter that defines
        sub-objects (e.g. ``http_json`` endpoints) still gets its secrets
        resolved and fail-closed-checked at startup.
        """
        found: dict[str, str] = {}

        def walk(prefix: str, obj: Any) -> None:
            if isinstance(obj, dict):
                for key, value in obj.items():
                    path = f"{prefix}.{key}" if prefix else str(key)
                    if (
                        isinstance(key, str)
                        and key.endswith("_env")
                        and isinstance(value, (str, int))
                        and not isinstance(value, bool)
                    ):
                        found[path] = str(value)
                    else:
                        walk(path, value)
            elif isinstance(obj, list):
                for i, value in enumerate(obj):
                    walk(f"{prefix}.{i}", value)

        walk("", dict(self.options))
        return found


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    adapters: dict[str, AdapterConfig]
    source: Path | None = None

    def enabled_adapters(self) -> list[AdapterConfig]:
        return [a for a in self.adapters.values() if a.enabled]

    def resolve_secrets(
        self, environ: Mapping[str, str] | None = None
    ) -> dict[str, dict[str, str]]:
        """Resolve every enabled adapter's ``*_env`` fields against *environ*.

        Returns ``{adapter_name: {env_var_name: value}}``. Raises
        :class:`ConfigError` listing every missing variable across all enabled
        adapters, so the operator sees the whole gap at once.

        With no explicit *environ*, the process-wide credential source is used:
        a value comes from ``$CREDENTIALS_DIRECTORY/<name>`` (or another creds
        directory) before ``os.environ``, so a secret need never sit in the
        process environment.
        """
        env: Mapping[str, str] = default_credential_source() if environ is None else environ
        resolved: dict[str, dict[str, str]] = {}
        missing: list[str] = []
        for adapter in self.enabled_adapters():
            per_adapter: dict[str, str] = {}
            for field_name, var_name in adapter.secret_env_fields().items():
                if var_name not in env or env[var_name] == "":
                    missing.append(f"{adapter.name}.{field_name} -> ${var_name}")
                else:
                    per_adapter[var_name] = SecretStr(env[var_name])
            resolved[adapter.name] = per_adapter
        if missing:
            raise ConfigError(
                "missing secret environment variable(s) for enabled adapter(s): "
                + ", ".join(sorted(missing))
            )
        return resolved


def _parse_server(raw: Any) -> ServerConfig:
    if raw is None:
        return ServerConfig()
    if not isinstance(raw, dict):
        raise ConfigError("[server] must be a table")
    unknown = set(raw) - _SERVER_KEYS
    if unknown:
        raise ConfigError(f"[server] has unknown key(s): {', '.join(sorted(unknown))}")

    transport = raw.get("transport", "stdio")
    if transport not in VALID_TRANSPORTS:
        raise ConfigError(
            f"[server].transport {transport!r} is not one of {', '.join(VALID_TRANSPORTS)}"
        )

    ttl = raw.get("cache_ttl_seconds", 15.0)
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl < 0:
        raise ConfigError("[server].cache_ttl_seconds must be a number >= 0")

    host = raw.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host:
        raise ConfigError("[server].host must be a non-empty string")

    port = raw.get("port", 8000)
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ConfigError("[server].port must be an integer in 1-65535")

    http_path = raw.get("http_path", "/mcp")
    if not isinstance(http_path, str) or not http_path.startswith("/"):
        raise ConfigError("[server].http_path must be a string starting with '/'")

    log_level = str(raw.get("log_level", "INFO")).upper()
    if log_level not in VALID_LOG_LEVELS:
        raise ConfigError(
            f"[server].log_level {log_level!r} is not one of {', '.join(VALID_LOG_LEVELS)}"
        )

    allowed_hosts = _str_list(raw.get("allowed_hosts"), "[server].allowed_hosts")
    allowed_origins = _str_list(raw.get("allowed_origins"), "[server].allowed_origins")

    return ServerConfig(
        transport=transport,
        cache_ttl_seconds=float(ttl),
        host=host,
        port=port,
        http_path=http_path,
        log_level=log_level,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _str_list(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{label} must be a list of strings")
    return tuple(value)


def _parse_adapters(raw: Any) -> dict[str, AdapterConfig]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("[adapters] must be a table")
    out: dict[str, AdapterConfig] = {}
    for name, table in raw.items():
        if not isinstance(table, dict):
            raise ConfigError(f"[adapters.{name}] must be a table")
        if "enabled" not in table:
            raise ConfigError(f"[adapters.{name}] is missing the required 'enabled' key")
        enabled = table["enabled"]
        if not isinstance(enabled, bool):
            raise ConfigError(f"[adapters.{name}].enabled must be a boolean")
        options = {k: v for k, v in table.items() if k != "enabled"}
        out[name] = AdapterConfig(name=name, enabled=enabled, options=options)
    return out


def load_config_data(data: Mapping[str, Any], *, source: Path | None = None) -> Config:
    """Build a :class:`Config` from an already-parsed mapping (used by tests)."""
    unknown = set(data) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise ConfigError(f"config has unknown top-level key(s): {', '.join(sorted(unknown))}")
    return Config(
        server=_parse_server(data.get("server")),
        adapters=_parse_adapters(data.get("adapters")),
        source=source,
    )


def load_config_text(text: str, *, source: Path | None = None) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config is not valid TOML: {exc}") from exc
    return load_config_data(data, source=source)


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Load config from *path*, or ``$HOMELAB_MCP_CONFIG`` if *path* is ``None``."""
    env = os.environ if environ is None else environ
    if path is None:
        env_path = env.get(CONFIG_ENV_VAR)
        if not env_path:
            raise ConfigError("no config path given and $HOMELAB_MCP_CONFIG is not set")
        path = env_path
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    return load_config_text(config_path.read_text(encoding="utf-8"), source=config_path)
