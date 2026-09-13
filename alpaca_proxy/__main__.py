"""Entry point: python -m alpaca_proxy [--config PATH]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from . import __version__
from .config import Config, load_config
from .poller import Poller
from .server import create_app
from .upstream import UpstreamClient

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_BACKUP_DAYS = 14
NOISY_LOGGERS = ("httpx", "httpcore")

log = logging.getLogger("alpaca_proxy")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="alpaca-proxy",
        description="Caching Alpaca proxy for a remote safety monitor and weather station.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="path to the YAML config file (default: ./config.yaml)",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def _setup_logging(config: Config) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(stream=sys.stderr)]
    if config.log_file:
        path = Path(config.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Daily rotation, not size-based: the rig images nightly and the PC shuts
        # down at dawn, so one file per night keeps a bad night easy to find.
        # TimedRotatingFileHandler seeds its next rollover from the existing
        # file's mtime, so an evening restart appends to that day's file.
        handlers.append(
            logging.handlers.TimedRotatingFileHandler(
                path,
                when="midnight",
                backupCount=LOG_BACKUP_DAYS,
                encoding="utf-8",
            )
        )
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        handlers=handlers,
        force=True,
    )
    # httpx logs a line per upstream request (~12 every poll interval), which
    # rotates the proxy's own diagnostics out of the log within hours. Floor the
    # noisy third-party loggers at WARNING unless the operator asked for DEBUG.
    if level > logging.DEBUG:
        for name in NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)


def build_app(config: Config) -> FastAPI:
    """Wire client + poller into an app whose lifespan owns the poll loop."""
    client = UpstreamClient(
        base_url=config.upstream_base_url,
        client_id=config.client_id,
        timeout=config.request_timeout_seconds,
    )
    poller = Poller(config, client)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(poller.run(), name="alpaca-proxy-poll")
        log.info(
            "polling %s every %.1fs (failure threshold %d)",
            config.upstream_base_url,
            config.poll_interval_seconds,
            config.failure_threshold,
        )
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await client.aclose()
            log.info("poll loop stopped, upstream client closed")

    return create_app(config, poller, lifespan=lifespan)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
    except ValueError as exc:
        print(f"alpaca-proxy: {exc}", file=sys.stderr)
        return 2

    _setup_logging(config)
    log.info("alpaca-proxy %s starting on %s:%d", __version__, config.host, config.port)

    # uvicorn installs its own SIGINT/SIGTERM handlers; both trigger the
    # lifespan shutdown above, which cancels the poll task and closes httpx.
    uvicorn.run(
        build_app(config),
        host=config.host,
        port=config.port,
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
