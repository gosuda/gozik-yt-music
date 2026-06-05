"""
server.py — Application entrypoint for the gozik YouTube Music plugin server.

Starts a gRPC server bound to 127.0.0.1:50051. Designed to run as a
persistent background daemon alongside the gozik desktop player.

Usage:
    python3 server.py [--port PORT] [--workers N]

Signals:
    SIGTERM / SIGINT — graceful shutdown with a configurable drain timeout.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from concurrent import futures
from pathlib import Path

import grpc

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so both `generated` and `handlers`
# packages are importable regardless of the working directory.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from generated import music_provider_pb2_grpc  # noqa: E402
from handlers.provider import MusicProviderServicer  # noqa: E402

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("gozik.ytmusic.server")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 50051
DEFAULT_WORKERS = 4
GRACEFUL_SHUTDOWN_TIMEOUT_S = 10


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="gozik YouTube Music gRPC plugin server"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("GOZIK_YTM_HOST", DEFAULT_HOST),
        help=f"Bind host (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("GOZIK_YTM_PORT", DEFAULT_PORT)),
        help=f"Bind port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("GOZIK_YTM_WORKERS", DEFAULT_WORKERS)),
        help=f"gRPC thread-pool worker count (default: {DEFAULT_WORKERS})",
    )
    return parser.parse_args()


def serve(host: str, port: int, workers: int) -> None:
    """Configure and run the gRPC server until a termination signal is received."""

    bind_address = f"{host}:{port}"
    logger.info("Initialising gRPC server on %s with %d worker threads", bind_address, workers)

    # Build the thread-pool backed gRPC server.
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=workers),
        options=[
            # Allow large payloads (playlist responses can be substantial).
            ("grpc.max_send_message_length", 64 * 1024 * 1024),
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),
            # Keep-alive settings for long-lived daemon connections.
            ("grpc.keepalive_time_ms", 30_000),
            ("grpc.keepalive_timeout_ms", 10_000),
            ("grpc.keepalive_permit_without_calls", True),
        ],
    )

    # Register the MusicProviderService implementation.
    servicer = MusicProviderServicer()
    music_provider_pb2_grpc.add_MusicProviderServiceServicer_to_server(servicer, server)

    # Bind to loopback only — this daemon is never exposed externally.
    server.add_insecure_port(bind_address)
    server.start()
    logger.info("gRPC server listening on %s", bind_address)

    # ---------------------------------------------------------------------------
    # Graceful shutdown handler — triggered by SIGTERM or SIGINT (Ctrl-C).
    # ---------------------------------------------------------------------------
    _stop_event = {"triggered": False}

    def _handle_signal(signum: int, _frame: object) -> None:
        sig_name = signal.Signals(signum).name
        if _stop_event["triggered"]:
            logger.warning("Second %s received — forcing immediate exit", sig_name)
            sys.exit(1)
        logger.info("Received %s — initiating graceful shutdown (timeout: %ds)", sig_name, GRACEFUL_SHUTDOWN_TIMEOUT_S)
        _stop_event["triggered"] = True
        server.stop(GRACEFUL_SHUTDOWN_TIMEOUT_S)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Block the main thread.
    try:
        while not _stop_event["triggered"]:
            time.sleep(1)
    finally:
        logger.info("Server shutdown complete")


def main() -> None:
    """Entry point."""
    args = _parse_args()
    serve(host=args.host, port=args.port, workers=args.workers)


if __name__ == "__main__":
    main()
