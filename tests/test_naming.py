from __future__ import annotations

import pytest

from homelab_mcp.naming import FORBIDDEN_TOOL_VERBS, tool_name_violation


@pytest.mark.parametrize(
    "name",
    [
        "proxmox_list_nodes",
        "proxmox_guest_status",
        "netbox_find_device",
        "netbox_device_by_ip",
        "systemd_failed_units",
        "systemd_recent_log",
        "grafana_list_alerts",
        "adguard_top_clients",
    ],
)
def test_read_only_names_pass(name: str) -> None:
    assert tool_name_violation(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "restart_service",
        "delete_guest",
        "set_config",
        "proxmox_start_guest".replace("proxmox_", ""),  # 'start_guest'
        "update_record",
        "rotate_token",
        "run_command",
        "power_cycle",
    ],
)
def test_mutating_names_rejected(name: str) -> None:
    assert tool_name_violation(name) is not None


@pytest.mark.parametrize("name", ["Proxmox_List", "list nodes", "list-nodes", ""])
def test_malformed_names_rejected(name: str) -> None:
    assert tool_name_violation(name) is not None


def test_every_forbidden_verb_is_caught_as_a_prefix() -> None:
    for verb in FORBIDDEN_TOOL_VERBS:
        assert tool_name_violation(f"{verb}_something") is not None
