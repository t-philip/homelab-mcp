"""Command-line entry point: ``homelab-mcp run`` and ``homelab-mcp doctor``."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
from collections.abc import Sequence

from . import __version__, secret_redaction
from .config import VALID_LOG_LEVELS, VALID_TRANSPORTS, Config, load_config
from .errors import HomelabMcpError
from .registry import iter_adapter_probes
from .server import run_server


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="homelab-mcp",
        description="Read-only MCP server for homelab infrastructure.",
    )
    parser.add_argument("--version", action="version", version=f"homelab-mcp {__version__}")
    parser.add_argument(
        "--log-level",
        choices=VALID_LOG_LEVELS,
        help="override [server].log_level",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the MCP server")
    run.add_argument(
        "-c", "--config", help="path to the config file (default: $HOMELAB_MCP_CONFIG)"
    )
    run.add_argument("--transport", choices=VALID_TRANSPORTS, help="override [server].transport")
    run.add_argument("--host", help="override [server].host (sse / streamable-http only)")
    run.add_argument("--port", type=int, help="override [server].port (sse / streamable-http only)")

    doctor = sub.add_parser("doctor", help="probe every enabled adapter and report")
    doctor.add_argument(
        "-c", "--config", help="path to the config file (default: $HOMELAB_MCP_CONFIG)"
    )

    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Attach the value-redaction filter now (empty); it is seeded with the
    # resolved adapter secrets in build_adapters().
    secret_redaction.install()


def _load(
    config_path: str | None,
    *,
    transport: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> Config:
    config = load_config(config_path)
    if transport is None and host is None and port is None:
        return config
    server = dataclasses.replace(
        config.server,
        transport=transport if transport is not None else config.server.transport,
        host=host if host is not None else config.server.host,
        port=port if port is not None else config.server.port,
    )
    return dataclasses.replace(config, server=server)


async def _doctor(config: Config) -> int:
    results = [r async for r in iter_adapter_probes(config)]
    if not results:
        print("no adapters are enabled")
        return 1

    width = max(len(r.adapter) for r in results)
    all_ok = True
    for result in sorted(results, key=lambda r: r.adapter):
        mark = "ok  " if result.ok else "FAIL"
        all_ok &= result.ok
        print(f"{mark}  {result.adapter.ljust(width)}  {result.detail}")
    return 0 if all_ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = _load(
            args.config,
            transport=getattr(args, "transport", None),
            host=getattr(args, "host", None),
            port=getattr(args, "port", None),
        )
        _configure_logging(args.log_level or config.server.log_level)

        if args.command == "run":
            asyncio.run(run_server(config))
            return 0
        if args.command == "doctor":
            return asyncio.run(_doctor(config))
    except HomelabMcpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    return 2  # pragma: no cover - argparse requires a subcommand


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
