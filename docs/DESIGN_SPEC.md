# homelab-mcp — Design Specification

**Status:** v1.0.0 — all eight initial adapters (28 read-only tools).
**Last reconciled to code:** v1.0.0.

This document explains how homelab-mcp is built and why. It is kept honest
against the code: where the build does not yet match the intent, §0.1 says so.

---

## 0. Purpose

Give an MCP client read-only visibility into homelab infrastructure through a
small set of typed, documented tools — so an assistant can answer *"is that
container up?"* or *"which VMs are heaviest on memory?"* with one tool call
instead of a reconstructed shell pipeline.

homelab-mcp is a standalone server. Any MCP client (an assistant, a bot, an IDE)
can consume it. This repo is **only** the server,
and it is **only** read-only.

### 0.1 Deviations from intent (as of v1.0.0)

| Area | Intended | As built | Status |
|---|---|---|---|
| Adapters | proxmox, netbox, systemd, grafana, influxdb, adguard, pinglog, http_json | all eight shipped (28 tools) | **done** |
| Adapter crash paths | a bad upstream shape or a bad config value degrades to a tool/`doctor` error, never a server crash (A5) | `proxmox` tolerates Proxmox's `{"data": null}` envelope (audit-only tokens return it) via `_data_list` / `_data_dict`; `pinglog` `_parse_time` handles millisecond epochs, `inf`/`nan` and out-of-range values (→ skipped, not raised) and sorts `target_history` chronologically; `http_json` validates each endpoint URL at startup (`ConfigError`, not a first-call `httpx.InvalidURL` crash); numeric config options (`pinglog.max_bytes`, `systemd.command_timeout_seconds`) are checked in `validate()` and read leniently in `__init__` (`base.opt_number` / `require_number`) | **done** (found by a line-by-line review) |
| Read-only audit coverage | the static non-GET audit sees every HTTP call | `_audit` now scans whole-file (not line-by-line), so InfluxDB's multi-line `client.stream("POST", …)` — the one allow-listed non-GET — is actually detected and the allowlist entry is exercised (test asserts it) | **done** (was: multi-line calls invisible) |
| Transports | stdio + streamable-http/sse, user-selectable | all three wired; in-memory `mcp.Client` protocol round-trip **and** an ASGI HTTP round-trip (initialize → tools/list) are tested; the SDK's DNS-rebinding guard is fed an allow-list from config (without it the HTTP transport 421s every request) | **done** |
| `netbox` tools | find_device, list_devices, device_by_ip, ip_usage | all four | **done** |
| Packaging | `pipx install homelab-mcp` from a published release | builds locally with hatchling; not published | pre-release |
| License | AGPL-3.0-or-later | `LICENSE` present, `pyproject` `license = "AGPL-3.0-or-later"` | **done** |
| MCP SDK | official MCP SDK, current | `mcp>=2,<3` (`MCPServer`, snake_case annotation fields, `ToolError` for expected failures) | **done** — all SDK use is in `server.py` |
| Fail-closed at startup | missing secret *or* required endpoint aborts `run` | both — each adapter declares `required_options`, checked in `build_adapters` alongside secrets | **done** (was: only secrets) |
| Read-only enforcement | naming rule + annotations | plus a static audit (`_audit.find_non_get_http_calls`) run in the CI lint and a unit test: no non-GET HTTP call outside a one-entry allowlist | **done** |
| Daemon safety | bounded resources, a timeout on every call | `TTLCache` bounded + self-sweeping; systemd commands wrapped in `asyncio.wait_for`; every HTTP response read with an 8 MiB cap | **done** |
| Observability | logs on a 24/7 service | stdlib `logging` — startup/shutdown, every `UpstreamError` / tool `ToolError`, TLS-off and public-bind warnings; level from `[server].log_level` / `--log-level` | **done** (was: none) |
| Public-repo privacy tooling | `check_privacy.sh` + `.githooks/pre-push` + `check-privacy.yml` + `.privacy-denylist` | present and active | **done** |

---

## 1. Architecture

```
config (TOML)  ──►  Config          ──►  build_adapters()  ──►  LoadedAdapters
                     · [server]            · resolve secrets       · adapters[]
                     · [adapters.*]        · instantiate each      · tools[]  (ToolSpec)
                                           · reject write-tool names
                                                   │
                                                   ▼
                                            build_server()  ──►  MCPServer  (mcp >= 2)
                                             · add_tool(wrapped_handler,
                                             ·          read_only_hint=True)
                                             · wrapper: HomelabMcpError -> ToolError
                                                   │
                                     run_stdio_async() / run_streamable_http_async()
                                                   / run_sse_async()
```

Module map (`src/homelab_mcp/`):

| Module | Responsibility |
|---|---|
| `config.py` | Parse + validate the TOML. Resolve `*_env` fields against the environment; fail closed on any missing secret for an enabled adapter. |
| `cache.py` | `TTLCache` — dedupe identical calls within an agent turn. Not correctness-critical. |
| `naming.py` | `tool_name_violation()` — the read-only rule, expressed as a forbidden set of leading verbs. |
| `adapters/base.py` | `Adapter` ABC, `AdapterOptions`, `ToolSpec`, `ProbeResult`. |
| `adapters/_http.py` | Shared async HTTP client builder + `get_json` (maps failures to `UpstreamError`). |
| `adapters/{proxmox,netbox,systemd,grafana,influxdb,adguard,pinglog,http_json}.py` | One backing system each. |
| `registry.py` | `build_adapters()` — instantiate enabled adapters, collect + validate tools. |
| `server.py` | `build_server()` / `build_http_app()` / `run_server()` — bind tools into an `MCPServer`, translate `HomelabMcpError` → SDK `ToolError`, run the chosen transport. **The only module that imports `mcp`.** |
| `cli.py` | `homelab-mcp run` / `homelab-mcp doctor`, with `--transport` / `--host` / `--port` overrides. |

---

## 2. The read-only guarantee

The single most important property: **this server cannot change anything.**

It is enforced at four layers:

1. **Registry** (`build_adapters`) rejects any tool whose name violates
   `naming.tool_name_violation` — checked at server startup.
2. **Unit test** `tests/test_no_write_tools.py` runs the same check over every
   adapter's declared tools.
3. **CI lint** `scripts/check_no_write_tools.py` — a standalone exit-code check.
4. **Static HTTP audit** (`homelab_mcp._audit.find_non_get_http_calls`, run by
   the CI lint and `tests/test_read_only_http.py`) scans adapter source for
   `.post/.put/.patch/.delete/.stream("POST"…)` and fails unless each is on an
   explicit allowlist. The only entry is InfluxDB's `POST /api/v2/query`, which
   is a read. This closes the gap that the naming rule alone left: a tool with
   a read verb in its name whose handler quietly did `client.post(...)`.

The name rule is about the *leading verb*: a read tool is named for what it
returns (`list_nodes`, `guest_status`), and any name starting with `restart`,
`delete`, `set`, `create`, … (see `FORBIDDEN_TOOL_VERBS`) is refused regardless
of what its code does. Every tool is also registered with
`ToolAnnotations(read_only_hint=True, destructive_hint=False)`.

Write actions are out of scope for this project by design. If an assistant needs
to *act*, that belongs in a separate, separately-reviewed server.

---

## 3. Configuration & secrets

TOML, one `[adapters.<name>]` table per backend, each with `enabled`.

Secrets are never stored in the file. A field ending in `_env` holds the *name*
of an environment variable; `Config.resolve_secrets()` reads the values at
startup and raises `ConfigError` listing **every** missing variable across all
enabled adapters at once. A disabled adapter's missing variables are ignored.

This "fail closed if a secret is absent" behaviour is deliberate: a
half-configured server that silently omits an adapter is worse than one that
refuses to start.

---

## 4. Adapters

Each adapter:

* is constructed from `AdapterOptions` with **no I/O in `__init__`** (clients are
  built lazily), so tools can be enumerated for the lint without network or
  credentials;
* exposes `tools() -> list[ToolSpec]` (pure, no I/O);
* exposes `probe() -> ProbeResult` that **never raises** — a down backend
  reports `ok=False`, it does not crash `doctor` or the server;
* wraps every backing call so upstream failures become `UpstreamError`
  (surfaced to the client as a tool error).

### 4.1 proxmox

`GET`-only against the Proxmox VE API with an API **token**
(`Authorization: PVEAPIToken=<id>=<secret>`). The token needs only audit roles.

| Tool | Backing call |
|---|---|
| `proxmox_list_nodes` | `/api2/json/nodes` |
| `proxmox_list_guests` | `/api2/json/cluster/resources?type=vm` (+ in-code filter by node/type) |
| `proxmox_guest_status` | cluster resources to resolve node/type, then `/nodes/{node}/{type}/{vmid}/status/current` |
| `proxmox_node_resources` | `/api2/json/nodes/{node}/status` |

Values are summarised (CPU%, memory%, byte totals, uptime) rather than passed
through raw.

### 4.2 netbox

`GET`-only against the NetBox REST API (`Authorization: Token <token>`), a
read-only token is sufficient.

| Tool | Backing call |
|---|---|
| `netbox_find_device` | `/api/dcim/devices/?name__ie=` then `?name=` |
| `netbox_list_devices` | `/api/dcim/devices/?role=&site=&status=&limit=` (limit clamped 1–200) |
| `netbox_device_by_ip` | `/api/ipam/ip-addresses/?address=` |
| `netbox_ip_usage` | `/api/ipam/prefixes/?prefix=` + `/api/ipam/ip-addresses/?parent=&limit=1`; size from `ipaddress.ip_network`, so `used` / `free` / `utilization_pct` are computed here |

Handles both the modern `role` and legacy `device_role` keys. `netbox_ip_usage`
validates the CIDR with a regex and `ipaddress` before it goes anywhere near a
query.

### 4.3 systemd

Runs a **fixed** set of read-only commands over SSH: `systemctl show`,
`systemctl --failed`, `journalctl`. Never a shell string — an argv list.

* Host must be in the configured `hosts` allowlist or the call is rejected.
* Unit name must match `^[A-Za-z0-9@._:\-]+$`.
* `recent_log` clamps `lines` to 1–500.
* The command runner is injectable (`Runner`); tests never touch SSH.

| Tool | Command |
|---|---|
| `systemd_service_status` | `systemctl show <unit> --property=Id,LoadState,ActiveState,SubState,UnitFileState,…` |
| `systemd_failed_units` | `systemctl --failed --no-legend --plain --no-pager` |
| `systemd_recent_log` | `journalctl -u <unit> -n <lines> --no-pager -o short-iso` |

### 4.4 grafana

`GET`-only against the Grafana HTTP API (`Authorization: Bearer <token>`); a
Viewer service-account token suffices.

| Tool | Backing call |
|---|---|
| `grafana_list_dashboards` | `/api/search?type=dash-db` (+ optional `query` / `tag`) |
| `grafana_dashboard` | `/api/dashboards/uid/{uid}` → title, folder, panel titles |
| `grafana_list_alerts` | `/api/prometheus/grafana/api/v1/rules` → rule name/group/state/active count |
| `grafana_health` | `/api/health` |

### 4.5 influxdb

InfluxDB v2 (`Authorization: Token <token>`). **The model never supplies raw
Flux.** Every query is assembled from parts that are each validated:
bucket/measurement/field must match `^[A-Za-z0-9_.:\-/]+$`, tag keys match
`^[A-Za-z0-9_]+$`, a value that reaches a filter predicate may not contain
`"` `\` backtick, a control character, or `$` `{` `}` (which would let Flux
`${...}` string interpolation run inside the literal), and the time range must
be a negative Flux duration between `-1h` and `-400d`. Results come back as
annotated CSV, are parsed into row dicts, and are cached like the other
adapters. `POST /api/v2/query` is the codebase's one non-GET call and is a
read — it is on the `_audit` allowlist.

| Tool | Query |
|---|---|
| `influxdb_list_buckets` | `GET /api/v2/buckets?org=` |
| `influxdb_measurements` | `schema.measurements(bucket, start)` |
| `influxdb_field_keys` | `schema.measurementFieldKeys(bucket, measurement, start)` |
| `influxdb_latest_value` | `from(bucket) |> range(start) |> filter(_measurement[, _field, tags]) |> last() |> limit(500)` |

### 4.6 adguard

AdGuard Home control API over HTTP Basic auth.

| Tool | Backing call |
|---|---|
| `adguard_status` | `/control/status` |
| `adguard_stats` | `/control/stats` (aggregate counts, avg processing time) |
| `adguard_top_clients` | `/control/stats` → `top_clients` (cached with the others) |
| `adguard_top_domains` | `/control/stats` → `top_queried_domains` + `top_blocked_domains` |
| `adguard_query_log` | `/control/querylog?limit=&search=` (limit clamped 1–100) |

### 4.7 pinglog

Reads a **local** availability log — CSV (with a header) or JSONL. Column names
(`time_column`, `target_column`, `success_column` / `rtt_column`) are
configurable so it fits whatever a given monitor writes. Only the last
`max_bytes` (default 5 MiB) of the file is read; if the tail has no header the
file is re-read once from the top for the header row. Fixture timestamps in
tests are built from `now − timedelta(...)` — relative, so they carry no
calendar fact (the privacy rule's explicit carve-out).

| Tool | Result |
|---|---|
| `pinglog_summary` | per-target `{samples, up, down, unknown, uptime_pct, last_seen, last_status}` over the last N minutes |
| `pinglog_target_history` | the last N raw samples for one target |

### 4.8 http_json

Fetches an endpoint the operator **named in the config** — the model passes a
name, never a URL, so there is no SSRF surface. Each endpoint may carry static
headers, a `token_env` (→ `Authorization: Bearer`), and an `extract` dotted path
(`a.b.0.c`) that pulls one sub-value out of the response. Nested `token_env`
fields are picked up by the recursive secret resolver, so a missing one still
fails the server closed at startup.

| Tool | Behaviour |
|---|---|
| `http_json_list_endpoints` | names + URLs of configured endpoints (no secrets) |
| `http_json_get` | GET the named endpoint; return its JSON, or the `extract`ed value |

---

## 5. Transports

The user picks one in `[server].transport` (or `--transport`):

* **`stdio`** (default): no network port; the client spawns the server as a
  subprocess. Implemented; the assembled tool set is smoke-tested and a tool
  round-trip (happy path + error translation) is covered.
* **`streamable-http`**: for a client on another host. `host` / `port` /
  `http_path` in config (or `--host` / `--port`). `build_http_app()` returns the
  raw Starlette app for hosting behind an ASGI server. A test drives the app
  over ASGI (`initialize` → `tools/list`). **Bind to LAN/VPN only** — the
  default `host` is `127.0.0.1` on purpose.
* **`sse`**: the legacy HTTP transport, wired through the SDK's runner.

**DNS-rebinding guard.** The SDK enables Host-header checking by default and,
with an empty allow-list, returns HTTP 421 to *every* request. `server.py`
therefore always passes a `TransportSecuritySettings` built from
`[server].allowed_hosts` / `allowed_origins`; when those are unset it defaults
to `localhost`, `127.0.0.1`, `[::1]` and the configured `host`, each with and
without `:port`. Set `allowed_hosts` when a client connects by LAN name.

---

## 6. Testing

`uv run pytest` — currently 140 tests, all green. Coverage:

* `naming` — read-only names pass, mutating/malformed names rejected, every
  forbidden verb caught as a prefix.
* `cache` — hit/miss, expiry (monotonic clock patched), `ttl=0` disables,
  concurrent miss calls the factory exactly once.
* `config` — parsing, every rejection path, `resolve_secrets` reports all gaps
  (including `*_env` fields nested inside `http_json` endpoints), the shipped
  `homelab-mcp.example.toml` is valid and ships everything disabled.
* `no_write_tools` — the guarantee, over the real adapter set (28 tools).
* adapters — `respx`-mocked HTTP against recorded fixtures for proxmox / netbox /
  grafana / adguard / influxdb / http_json (summary shape, filters, caching,
  auth header, error paths); influxdb also asserts the generated Flux is safe
  and rejects quote/duration/tag-key injection; systemd uses an injected fake
  runner (parsing, host allowlist, unit validation, clamping, caching); pinglog
  uses temp CSV/JSONL files with `now`-relative rows (windowing, uptime %,
  missing-file probe).
* `server` — adapters collected, missing-secret **and missing-required-option**
  fail closed, unknown adapter rejected, tools registered with read-only
  annotations, input schemas carry the documented required params, a tool call
  round-trips (happy path returns structured content; an adapter error becomes
  the SDK's `ToolError`, not `UnexpectedToolError`), and `build_http_app` mounts
  the configured path.
* `read_only_http` — the static HTTP audit finds no disallowed non-GET call and
  the allowlist is the expected single entry.
* `http_helpers` — oversized responses rejected (by `Content-Length` and while
  streaming); upstream error bodies are logged, never echoed in the message.
* `cli` — `--version`, subcommand required, missing config error, `doctor` with
  no enabled adapters, `doctor` shows a per-adapter FAIL row for a missing
  secret rather than aborting, `--transport` / `--host` / `--port` overrides.

**All fixture data is fictitious:** hosts `node-a` / `node-b` / `pmx-1`,
addresses in 192.0.2.0/24 and 198.51.100.0/24, free text `Test Data …`, every
absolute date before 2002 (relative-to-`now` fixtures stay relative). No real
infrastructure detail appears anywhere in this repo.

CI: `ruff check` + `ruff format --check` + `mypy --strict` + `pytest` +
`scripts/check_no_write_tools.py`. `scripts/check_privacy.sh` +
`.githooks/pre-push` + `.github/workflows/check-privacy.yml` are present and
ship with the repo.

---

## 7. Acceptance criteria

| # | Criterion | Verified? |
|---|---|---|
| A1 | No registered tool can change state; enforced in ≥2 independent ways | **Yes** — registry name check + test + lint + the static HTTP audit (non-GET allowlist) |
| A2 | Every tool carries `read_only_hint=True` | **Yes** (test_server) |
| A3 | An enabled adapter with a missing secret *or a missing required endpoint/option* prevents startup, naming the gap | **Yes** (test_config, test_server) |
| A4 | Adapters enumerate their tools with no network and no credentials | **Yes** (iter_declared_tools / lint) — `__init__` never validates or does I/O |
| A5 | A down backend degrades to a tool/`doctor` error, never a crash | **Yes** — `probe()` proven non-raising; adapter errors become SDK `ToolError`; systemd commands have an overall timeout; every HTTP body is size-capped |
| A6 | A client can complete the full MCP protocol (initialize → list → call) | **Yes** — in-memory `mcp.Client(server)` round-trip in `test_server` |
| A7 | HTTP transport actually serves MCP, and binds safely | **Yes** — ASGI round-trip test (`initialize` then `tools/list`); `127.0.0.1` default; a loud warning on a `0.0.0.0`/`::` bind; DNS-rebinding allow-list supplied from `[server].allowed_hosts` (default localhost + configured host) — without it the SDK 421s every request |
| A8 | `pipx install homelab-mcp` yields a working `homelab-mcp` command | **No** — not published |
| A9 | The server logs enough to diagnose a failure without a debugger | **Yes** — startup/shutdown, every `UpstreamError`/`ToolError`, TLS-off and public-bind warnings |

---

## 8. Open decisions

1. **`pinglog` schema autodetection** — currently the operator names the columns;
   could sniff common header names as a convenience.
2. **`allowed_origins` default** — empty (no browser MCP client expected); revisit
   if one appears.

*Resolved:* license → AGPL-3.0-or-later; MCP SDK → `mcp` 2.x
(`MCPServer`); transports → all three shipped, user-selectable, and round-trip
tested (in-memory + ASGI); all eight initial adapters built with all their tools
(`netbox_ip_usage` added; `http_json` = named-endpoint only, simple dotted
`extract`, no full JSONPath); every review finding applied.
