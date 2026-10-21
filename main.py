"""Entrypoint: load config.yaml and run the bridge's HTTP server with uvicorn.

Run natively, no containers required:

    python main.py [path/to/config.yaml]
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from bridge.config import ConfigError, load_config
from bridge.server import create_app


def main(argv: list[str]) -> int:
    try:
        config = load_config(argv[0] if argv else None)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=config.logging.level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # log_config=None lets uvicorn's loggers follow the root logger above.
    # timeout_graceful_shutdown: the relay holds WebSocket connections open
    # indefinitely, and without a limit stopping the bridge would wait for
    # every client to leave.
    uvicorn.run(
        create_app(config),
        host=config.bridge.listen_host,
        port=config.bridge.listen_port,
        log_config=None,
        timeout_graceful_shutdown=5,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
