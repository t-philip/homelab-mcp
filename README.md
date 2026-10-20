# homelab-mcp

A small [Model Context Protocol](https://modelcontextprotocol.io) server that
gives an MCP client (Claude Desktop, Claude Code, or any other) **read-only**
visibility into homelab infrastructure: Proxmox VE, NetBox, and systemd services
over SSH, with more backends to come.

> **Status: v1.0.0.** 8 adapters, 28 read-only tools. See `docs/DESIGN_SPEC.md`.

## Why

Asking an assistant *"is the reverse-proxy container up?"* or *"which VMs are
using the most memory?"* should be one clean tool call, not a reconstructed
`curl … | jq`. This server turns each backing system into a handful of typed,
documented, read-only tools.

## The read-only guarantee

Every tool only reads state. No adapter may register a tool whose name begins
with a state-changing verb (`restart`, `delete`, `set`, …). This is enforced in
three places: in the registry at startup, by a unit test over the real adapter
set, and by `scripts/check_no_write_tools.py` in CI. If you want an assistant to
*act*, that belongs in a separate, deliberately-scoped server — not this one.

## Install

```sh
pipx install homelab-mcp          # or: uv tool install homelab-mcp
```

## Configure

```sh
cp homelab-mcp.example.toml homelab-mcp.toml
$EDITOR homelab-mcp.toml
```

Secrets are never stored in the file. A field ending in `_env` names an
environment variable; the value is read from the environment at startup.

```sh
export NETBOX_TOKEN=...           # matches token_env in the config
homelab-mcp doctor -c homelab-mcp.toml   # probe every enabled adapter
```

## Run

```sh
homelab-mcp run -c homelab-mcp.toml
```

### Transports — your choice

- **`stdio`** (default) — no network port; the MCP client spawns the server as a
  subprocess. Register it with your client the usual way, e.g. for Claude Code:

  ```sh
  claude mcp add homelab -- homelab-mcp run -c /path/to/homelab-mcp.toml
  ```

- **`streamable-http`** (and legacy `sse`) — for when the client runs on a
  different host. Set `transport`, `host`, `port`, `http_path` in the config (or
  pass `--transport`/`--host`/`--port`), and **bind to a LAN or VPN address
  only** — never a public interface. `build_http_app()` also returns the raw
  Starlette app if you'd rather host it behind your own ASGI server.

  ```sh
  homelab-mcp run -c homelab-mcp.toml --transport streamable-http --host 192.0.2.10 --port 9000
  ```

## Adapters

| Adapter | Tools |
|---|---|
| `proxmox` | `proxmox_list_nodes`, `proxmox_list_guests`, `proxmox_guest_status`, `proxmox_node_resources` |
| `netbox` | `netbox_find_device`, `netbox_list_devices`, `netbox_device_by_ip`, `netbox_ip_usage` |
| `systemd` | `systemd_service_status`, `systemd_failed_units`, `systemd_recent_log` |
| `grafana` | `grafana_list_dashboards`, `grafana_dashboard`, `grafana_list_alerts`, `grafana_health` |
| `influxdb` | `influxdb_list_buckets`, `influxdb_measurements`, `influxdb_field_keys`, `influxdb_latest_value` |
| `adguard` | `adguard_status`, `adguard_stats`, `adguard_top_clients`, `adguard_top_domains`, `adguard_query_log` |
| `pinglog` | `pinglog_summary`, `pinglog_target_history` |
| `http_json` | `http_json_list_endpoints`, `http_json_get` |

The `influxdb` adapter never runs model-supplied Flux — every query is built
from validated bucket / measurement / field / tag parts. The `http_json`
adapter can only fetch endpoints named in the config, never an arbitrary URL.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check .
uv run mypy
uv run python scripts/check_no_write_tools.py
git config core.hooksPath .githooks    # once per clone — privacy pre-push hook
```

Logging goes to stderr; set the level with `[server].log_level` in the config
or `--log-level`. See `docs/PRIVACY.md` before pushing anything public.

## License

GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later). See
[`LICENSE`](LICENSE).
