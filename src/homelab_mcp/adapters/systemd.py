"""systemd adapter — read-only service state and logs over SSH.

Every command this adapter runs is one of a small fixed set
(``systemctl show``, ``systemctl --failed``, ``journalctl``). Host names are
checked against the configured allowlist and unit names against a strict
pattern (no leading dash — so a unit name can never be read as an option)
before anything is executed. The mechanism that actually runs a command is
injectable (:data:`Runner`) so tests never touch SSH.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from ..errors import ConfigError, HomelabMcpError, UpstreamError
from .base import (
    Adapter,
    AdapterOptions,
    ProbeResult,
    ToolSpec,
    int_arg,
    opt_number,
    require_number,
)

logger = logging.getLogger("homelab_mcp.systemd")

# Unit names: first character is not '-', so `systemctl`/`journalctl` can never
# parse the value as an option.
_UNIT_RE = re.compile(r"^[A-Za-z0-9@._:][A-Za-z0-9@._:\-]*$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._\-]+$")
_DEFAULT_SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=8")
_MAX_LOG_LINES = 500
_COMMAND_TIMEOUT = 20.0
_SHOW_PROPERTIES = (
    "Id,LoadState,ActiveState,SubState,UnitFileState,"
    "ActiveEnterTimestamp,ExecMainStartTimestamp,ExecMainStatus,Description"
)


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


#: ``(host, argv) -> CommandResult``. The default implementation runs ``ssh``.
Runner = Callable[[str, Sequence[str]], Awaitable[CommandResult]]


def _valid_hosts(options: AdapterOptions) -> list[str]:
    hosts = options.get("hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise ConfigError("[adapters.systemd].hosts must be a list of hostnames")
    if not hosts:
        raise ConfigError("[adapters.systemd].hosts must not be empty when enabled")
    for host in hosts:
        if not _HOST_RE.match(host):
            raise ConfigError(f"[adapters.systemd].hosts entry {host!r} is not a valid hostname")
    return hosts


def _default_runner(options: AdapterOptions) -> Runner:
    ssh_binary = str(options.get("ssh_binary", "ssh"))
    ssh_user = options.get("ssh_user")
    raw_opts = options.get("ssh_options", list(_DEFAULT_SSH_OPTIONS))
    ssh_options = (
        [str(o) for o in raw_opts] if isinstance(raw_opts, list) else list(_DEFAULT_SSH_OPTIONS)
    )
    timeout = opt_number(options, "command_timeout_seconds", _COMMAND_TIMEOUT, minimum=0.001)

    async def _run(host: str, argv: Sequence[str]) -> CommandResult:
        cmd = [ssh_binary, *ssh_options]
        if ssh_user:
            cmd += ["-l", str(ssh_user)]
        cmd += [host, "--", *argv]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise UpstreamError(f"ssh to {host} failed to start: {exc}") from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            with suppress(ProcessLookupError):
                await proc.wait()
            logger.warning("ssh %s %s timed out after %ss", host, argv[0], timeout)
            raise UpstreamError(f"command on {host} timed out after {timeout}s") from None
        return CommandResult(
            proc.returncode or 0,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
        )

    return _run


class SystemdAdapter(Adapter):
    name = "systemd"

    def __init__(self, options: AdapterOptions, runner: Runner | None = None) -> None:
        super().__init__(options)
        hosts = options.get("hosts", [])
        self._hosts = {h for h in hosts if isinstance(h, str)} if isinstance(hosts, list) else set()
        self._runner = runner or _default_runner(options)

    @classmethod
    def validate(cls, options: AdapterOptions) -> None:
        super().validate(options)
        _valid_hosts(options)
        require_number(options, "command_timeout_seconds", minimum=0.001)

    def _check_host(self, host: str) -> None:
        if host not in self._hosts:
            raise HomelabMcpError(
                f"host {host!r} is not in this adapter's allowlist "
                f"({', '.join(sorted(self._hosts))})"
            )

    @staticmethod
    def _check_unit(unit: str) -> None:
        if not _UNIT_RE.match(unit):
            raise HomelabMcpError(
                f"unit name {unit!r} is not a valid unit name "
                "(letters, digits, @ . _ : - ; no leading dash)"
            )

    async def probe(self) -> ProbeResult:
        if not self._hosts:
            return ProbeResult(self.name, False, "no hosts configured")
        host = sorted(self._hosts)[0]
        try:
            result = await self._runner(host, ["systemctl", "--version"])
        except HomelabMcpError as exc:
            return ProbeResult(self.name, False, str(exc))
        if result.exit_code != 0:
            return ProbeResult(
                self.name, False, f"{host}: systemctl --version exited {result.exit_code}"
            )
        first_line = result.stdout.splitlines()[0] if result.stdout else "ok"
        return ProbeResult(self.name, True, f"{host}: {first_line}")

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "systemd_service_status",
                "Get the state of one systemd unit on one host "
                "(active/sub state, unit-file state, last start time).",
                self.systemd_service_status,
            ),
            ToolSpec(
                "systemd_failed_units",
                "List systemd units in the failed state on one host.",
                self.systemd_failed_units,
            ),
            ToolSpec(
                "systemd_recent_log",
                "Return the most recent journald lines for one unit on one host.",
                self.systemd_recent_log,
            ),
        ]

    # -- tool handlers --------------------------------------------------

    async def systemd_service_status(self, host: str, unit: str) -> dict[str, Any]:
        """Get the state of one systemd unit.

        Args:
            host: a host from this adapter's configured allowlist.
            unit: the unit name (e.g. 'grafana-server.service').
        """
        self._check_host(host)
        self._check_unit(unit)
        argv = [
            "systemctl",
            "show",
            "--no-pager",
            f"--property={_SHOW_PROPERTIES}",
            "--",
            unit,
        ]
        result = await self._run_cached(host, argv)
        props: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                props[key.strip()] = value.strip()
        return {
            "host": host,
            "unit": props.get("Id") or unit,
            "description": props.get("Description") or None,
            "load_state": props.get("LoadState"),
            "active_state": props.get("ActiveState"),
            "sub_state": props.get("SubState"),
            "unit_file_state": props.get("UnitFileState"),
            "active": props.get("ActiveState") == "active",
            "active_enter_timestamp": props.get("ActiveEnterTimestamp") or None,
            "exec_main_start_timestamp": props.get("ExecMainStartTimestamp") or None,
            "exec_main_status": props.get("ExecMainStatus"),
        }

    async def systemd_failed_units(self, host: str) -> list[dict[str, Any]]:
        """List failed systemd units on a host.

        Args:
            host: a host from this adapter's configured allowlist.
        """
        self._check_host(host)
        argv = ["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"]
        result = await self._run_cached(host, argv)
        rows: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            unit, load, active, sub = parts[:4]
            description = parts[4] if len(parts) == 5 else ""
            rows.append(
                {
                    "unit": unit,
                    "load": load,
                    "active": active,
                    "sub": sub,
                    "description": description,
                }
            )
        return rows

    async def systemd_recent_log(self, host: str, unit: str, lines: int = 50) -> dict[str, Any]:
        """Return recent journald lines for a unit.

        Args:
            host: a host from this adapter's configured allowlist.
            unit: the unit name.
            lines: how many trailing lines to return (1-500).
        """
        self._check_host(host)
        self._check_unit(unit)
        n = int_arg(lines, "lines", lo=1, hi=_MAX_LOG_LINES)
        argv = ["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-iso"]
        result = await self._run_cached(host, argv)
        return {
            "host": host,
            "unit": unit,
            "lines": result.stdout.splitlines(),
        }

    # -- internals ----------------------------------------------------

    async def _run_cached(self, host: str, argv: Sequence[str]) -> CommandResult:
        key = ("systemd", host, tuple(argv))

        async def _call() -> CommandResult:
            return await self._runner(host, list(argv))

        return await self.options.cache.get_or_call(key, _call)
