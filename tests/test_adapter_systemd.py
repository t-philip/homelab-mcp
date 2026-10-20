from __future__ import annotations

from collections.abc import Sequence

import pytest

from homelab_mcp.adapters.systemd import CommandResult, SystemdAdapter
from homelab_mcp.errors import ConfigError, HomelabMcpError

from .conftest import load_fixture_text, make_options


class FakeRunner:
    """Returns canned output per (host, argv[0]); records every call."""

    def __init__(self, responses: dict[str, CommandResult]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, list[str]]] = []

    async def __call__(self, host: str, argv: Sequence[str]) -> CommandResult:
        self.calls.append((host, list(argv)))
        return self._responses.get(argv[0], CommandResult(0, "", ""))


def _adapter(runner: FakeRunner, hosts: list[str] | None = None) -> SystemdAdapter:
    return SystemdAdapter(
        make_options(raw={"hosts": hosts or ["node-a", "node-b"]}),
        runner=runner,
    )


async def test_service_status_parses_show_output() -> None:
    runner = FakeRunner(
        {"systemctl": CommandResult(0, load_fixture_text("systemd", "show_active.txt"), "")}
    )
    adapter = _adapter(runner)
    result = await adapter.systemd_service_status(host="node-a", unit="grafana-server.service")

    assert result["active"] is True
    assert result["active_state"] == "active"
    assert result["sub_state"] == "running"
    assert result["unit_file_state"] == "enabled"
    assert result["exec_main_status"] == "0"
    host, argv = runner.calls[0]
    assert host == "node-a"
    assert argv[:2] == ["systemctl", "show"]
    # the unit is passed after `--` so it can never be read as an option
    assert argv[-2:] == ["--", "grafana-server.service"]


async def test_failed_units_parsed() -> None:
    runner = FakeRunner(
        {"systemctl": CommandResult(1, load_fixture_text("systemd", "failed.txt"), "")}
    )
    adapter = _adapter(runner)
    rows = await adapter.systemd_failed_units(host="node-b")

    assert [r["unit"] for r in rows] == ["test-broken.service", "test-timer.timer"]
    assert rows[0]["active"] == "failed"
    assert rows[0]["description"].startswith("Test Data")


async def test_recent_log_clamps_and_returns_lines() -> None:
    runner = FakeRunner(
        {"journalctl": CommandResult(0, load_fixture_text("systemd", "journal.txt"), "")}
    )
    adapter = _adapter(runner)
    result = await adapter.systemd_recent_log(
        host="node-a", unit="grafana-server.service", lines=99999
    )

    assert len(result["lines"]) == 3
    _, argv = runner.calls[0]
    assert argv[0] == "journalctl"
    assert "500" in argv  # clamped to _MAX_LOG_LINES


async def test_host_not_in_allowlist_rejected() -> None:
    adapter = _adapter(FakeRunner({}))
    with pytest.raises(HomelabMcpError, match="allowlist"):
        await adapter.systemd_service_status(host="node-x", unit="whatever.service")


@pytest.mark.parametrize(
    "unit",
    ["foo.service; rm -rf /", "--version", "-M other", "bad unit", "../x"],
)
async def test_bad_unit_name_rejected(unit: str) -> None:
    adapter = _adapter(FakeRunner({}))
    with pytest.raises(HomelabMcpError, match="not a valid unit name"):
        await adapter.systemd_service_status(host="node-a", unit=unit)


async def test_empty_hosts_is_config_error() -> None:
    with pytest.raises(ConfigError, match="must not be empty"):
        SystemdAdapter.validate(make_options(raw={"hosts": []}))


async def test_non_list_hosts_is_config_error() -> None:
    with pytest.raises(ConfigError, match="list of hostnames"):
        SystemdAdapter.validate(make_options(raw={"hosts": "node-a"}))


async def test_command_timeout_surfaces_as_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from homelab_mcp.adapters.systemd import _default_runner
    from homelab_mcp.errors import UpstreamError

    class _HangingProc:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(10)
            return b"", b""

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return -9

    async def _fake_exec(*_a: object, **_k: object) -> _HangingProc:
        return _HangingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    runner = _default_runner(
        make_options(raw={"hosts": ["node-a"], "command_timeout_seconds": 0.05})
    )
    with pytest.raises(UpstreamError, match="timed out"):
        await runner("node-a", ["systemctl", "--version"])


async def test_default_runner_missing_ssh_binary_is_upstream_error() -> None:
    from homelab_mcp.adapters.systemd import _default_runner
    from homelab_mcp.errors import UpstreamError

    runner = _default_runner(
        make_options(raw={"hosts": ["node-a"], "ssh_binary": "definitely-not-a-real-binary-xyz"})
    )
    with pytest.raises(UpstreamError, match="failed to start"):
        await runner("node-a", ["systemctl", "--version"])


async def test_results_are_cached_per_command() -> None:
    runner = FakeRunner(
        {"systemctl": CommandResult(0, load_fixture_text("systemd", "show_active.txt"), "")}
    )
    adapter = _adapter(runner)
    await adapter.systemd_service_status(host="node-a", unit="grafana-server.service")
    await adapter.systemd_service_status(host="node-a", unit="grafana-server.service")
    assert len(runner.calls) == 1


async def test_probe_uses_first_host() -> None:
    runner = FakeRunner({"systemctl": CommandResult(0, "systemd 255 (255)\n+PAM +AUDIT", "")})
    adapter = _adapter(runner, hosts=["node-b", "node-a"])
    result = await adapter.probe()
    assert result.ok
    assert runner.calls[0][0] == "node-a"  # sorted -> first
    assert "systemd 255" in result.detail
