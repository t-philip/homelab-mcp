"""The adapter contract.

An adapter wraps one backing system (Proxmox, NetBox, ...). Construction is
cheap and never raises: ``__init__`` only stores :class:`AdapterOptions` and
initialises lazy handles. Config *shape* is checked separately by
:meth:`Adapter.validate`, which the registry calls at startup before
constructing the adapter — so tool enumeration never depends on a valid config.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from ..cache import TTLCache
from ..errors import ConfigError, HomelabMcpError


@dataclass(frozen=True)
class AdapterOptions:
    """Everything an adapter instance needs, resolved and ready.

    * ``raw`` is the ``[adapters.<name>]`` table minus ``enabled``.
    * ``secrets`` maps env-var name -> value, already resolved from the process
      environment for this adapter's ``*_env`` fields.
    * ``cache`` is shared across all adapters in a server.
    """

    raw: Mapping[str, Any]
    secrets: Mapping[str, str]
    cache: TTLCache

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def require(self, key: str) -> Any:
        if key not in self.raw or self.raw[key] in (None, ""):
            raise ConfigError(f"required option {key!r} is missing")
        return self.raw[key]

    def secret(self, env_field: str) -> str:
        """Resolve a ``*_env`` option: ``raw[env_field]`` names an env var."""
        var_name = self.raw.get(env_field)
        if not var_name:
            raise ConfigError(f"required option {env_field!r} is missing")
        try:
            return self.secrets[str(var_name)]
        except KeyError as exc:  # pragma: no cover - resolve_secrets guards this
            raise ConfigError(
                f"secret env var ${var_name} for {env_field!r} was not resolved"
            ) from exc


@dataclass(frozen=True)
class ToolSpec:
    """A single read-only tool an adapter offers.

    ``handler`` is an ``async def`` with fully type-annotated parameters and a
    docstring; the MCP layer derives the input schema from its signature.
    """

    name: str
    description: str
    handler: Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ProbeResult:
    adapter: str
    ok: bool
    detail: str


def int_arg(value: Any, name: str, *, lo: int, hi: int) -> int:
    """Coerce a tool argument to a bounded int, raising a clean tool error.

    Handlers use this instead of a bare ``int(...)`` so that a non-numeric
    argument surfaces as a :class:`HomelabMcpError` (a tool error) rather than a
    ``ValueError`` (a server crash) when the MCP SDK does not pre-validate.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HomelabMcpError(f"{name} must be an integer, got {value!r}") from exc
    return max(lo, min(parsed, hi))


def opt_number(options: AdapterOptions, key: str, default: float, *, minimum: float) -> float:
    """Read a numeric config option, falling back to *default* on anything unusable.

    Safe to call from ``__init__`` — it never raises, honouring the contract that
    construction does no validation. Pair it with :func:`require_number` in
    ``validate()`` so a genuinely bad value is a clean ``ConfigError`` at startup
    rather than a silent fallback.
    """
    raw = options.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < minimum:
        return default
    return float(raw)


def require_number(options: AdapterOptions, key: str, *, minimum: float) -> None:
    """``validate()``-side check: if *key* is present it must be a number >= *minimum*."""
    if key not in options.raw:
        return
    raw = options.raw[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < minimum:
        raise ConfigError(f"option {key!r} must be a number >= {minimum}")


class Adapter(ABC):
    #: Stable adapter key, matching the ``[adapters.<name>]`` table name.
    name: ClassVar[str]

    #: Config keys that must be present and non-empty for an enabled adapter.
    #: Checked by :meth:`validate` at startup. ``*_env`` names are also checked
    #: against the process environment by ``Config.resolve_secrets``.
    required_options: ClassVar[tuple[str, ...]] = ()

    def __init__(self, options: AdapterOptions) -> None:
        self.options = options

    @classmethod
    def validate(cls, options: AdapterOptions) -> None:
        """Check config shape at startup. Override for extra checks (call super).

        Must not perform I/O.
        """
        for key in cls.required_options:
            if key not in options.raw or options.raw[key] in (None, ""):
                raise ConfigError(f"[adapters.{cls.name}] requires {key!r}")

    @abstractmethod
    async def probe(self) -> ProbeResult:
        """Cheaply check that the backing system is configured and reachable."""

    @abstractmethod
    def tools(self) -> list[ToolSpec]:
        """Return this adapter's read-only tools. Must not perform I/O."""

    async def aclose(self) -> None:
        """Release any resources (HTTP clients, subprocess pools)."""
        return None
