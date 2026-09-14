"""Turn a :class:`Config` into a set of live adapters and their tools.

This is also the last line of defence for the read-only guarantee: every tool
name is checked against :func:`homelab_mcp.naming.tool_name_violation` here, so a
mis-named tool fails server startup even if the lint and the unit test were
somehow skipped.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

from . import secret_redaction
from .adapters import ADAPTER_CLASSES
from .adapters.base import Adapter, AdapterOptions, ProbeResult, ToolSpec
from .cache import TTLCache
from .config import AdapterConfig, Config
from .credentials import default_credential_source
from .errors import ConfigError, HomelabMcpError
from .naming import tool_name_violation

logger = logging.getLogger("homelab_mcp.registry")


@dataclass
class LoadedAdapters:
    adapters: list[Adapter]
    tools: list[ToolSpec]

    async def aclose(self) -> None:
        for adapter in self.adapters:
            try:
                await adapter.aclose()
            except Exception:
                logger.warning("error closing adapter %s", adapter.name, exc_info=True)


def _adapter_class(name: str) -> type[Adapter]:
    cls = ADAPTER_CLASSES.get(name)
    if cls is None:
        raise ConfigError(
            f"[adapters.{name}] is enabled but no such adapter exists "
            f"(known: {', '.join(sorted(ADAPTER_CLASSES))})"
        )
    return cls


def build_adapters(
    config: Config,
    *,
    environ: Mapping[str, str] | None = None,
    cache: TTLCache | None = None,
) -> LoadedAdapters:
    """Instantiate every enabled adapter and collect its tools.

    Raises :class:`ConfigError` on the first problem — a missing secret, a
    missing required option, a bad adapter name, a mis-named tool. The server
    fails closed.
    """
    secrets = config.resolve_secrets(environ)
    secret_redaction.redact_secrets(
        value for per_adapter in secrets.values() for value in per_adapter.values()
    )
    shared_cache = cache or TTLCache(config.server.cache_ttl_seconds)

    adapters: list[Adapter] = []
    tools: list[ToolSpec] = []
    seen_names: set[str] = set()

    for adapter_cfg in config.enabled_adapters():
        cls = _adapter_class(adapter_cfg.name)
        options = AdapterOptions(
            raw=adapter_cfg.options,
            secrets=secrets.get(adapter_cfg.name, {}),
            cache=shared_cache,
        )
        cls.validate(options)
        adapter = cls(options)
        adapters.append(adapter)

        for spec in adapter.tools():
            reason = tool_name_violation(spec.name)
            if reason is not None:
                raise ConfigError(f"adapter {adapter_cfg.name!r} registered a bad tool: {reason}")
            if spec.name in seen_names:
                raise ConfigError(f"two adapters both register a tool named {spec.name!r}")
            seen_names.add(spec.name)
            tools.append(spec)

    logger.info(
        "loaded %d adapter(s), %d tool(s): %s",
        len(adapters),
        len(tools),
        ", ".join(a.name for a in adapters) or "none",
    )
    return LoadedAdapters(adapters=adapters, tools=tools)


async def iter_adapter_probes(
    config: Config, *, environ: Mapping[str, str] | None = None
) -> AsyncIterator[ProbeResult]:
    """Yield one :class:`ProbeResult` per enabled adapter, for ``doctor``.

    Unlike :func:`build_adapters`, a per-adapter config gap (missing secret or
    required option) becomes a FAIL row rather than aborting the whole run — so
    ``doctor`` stays useful when the config is incomplete.
    """
    env: Mapping[str, str] = default_credential_source() if environ is None else environ
    shared_cache = TTLCache(config.server.cache_ttl_seconds)

    for adapter_cfg in config.enabled_adapters():
        try:
            cls = _adapter_class(adapter_cfg.name)
        except ConfigError as exc:
            yield ProbeResult(adapter_cfg.name, False, str(exc))
            continue

        try:
            secrets = _resolve_one(adapter_cfg, env)
        except ConfigError as exc:
            yield ProbeResult(adapter_cfg.name, False, str(exc))
            continue

        options = AdapterOptions(raw=adapter_cfg.options, secrets=secrets, cache=shared_cache)
        try:
            cls.validate(options)
            adapter = cls(options)
        except HomelabMcpError as exc:
            yield ProbeResult(adapter_cfg.name, False, str(exc))
            continue

        try:
            yield await adapter.probe()
        finally:
            try:
                await adapter.aclose()
            except Exception:
                logger.warning("error closing adapter %s", adapter_cfg.name, exc_info=True)


def _resolve_one(adapter_cfg: AdapterConfig, env: Mapping[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for field_name, var_name in adapter_cfg.secret_env_fields().items():
        if var_name not in env or env[var_name] == "":
            missing.append(f"{field_name} -> ${var_name}")
        else:
            resolved[var_name] = env[var_name]
    if missing:
        raise ConfigError("missing secret env var(s): " + ", ".join(sorted(missing)))
    return resolved
