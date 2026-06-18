"""
server.py — Application entrypoint for the gozik YouTube Music plugin server.

Starts a gRPC server bound to 127.0.0.1:50052. Designed to run as a
persistent background daemon alongside the gozik desktop player.

Usage:
    python3 server.py [--port PORT] [--workers N] [--web-ui-port PORT]

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
# Skip this when running as a Nuitka onefile binary — everything is already
# bundled and the source path should not leak into the runtime environment.
# ---------------------------------------------------------------------------
if "__compiled__" not in globals():
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
DEFAULT_PORT = 50052
DEFAULT_WORKERS = 4
DEFAULT_WEBUI_PORT = 50053
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
    parser.add_argument(
        "--web-ui-port",
        type=int,
        default=int(os.environ.get("GOZIK_YTM_WEBUI_PORT", DEFAULT_WEBUI_PORT)),
        help=(
            f"Web UI HTTP port (default: {DEFAULT_WEBUI_PORT}). "
            "Set to 0 to disable the web UI."
        ),
    )
    parser.add_argument(
        "--register-desktop-entry",
        choices=["auto", "always", "never"],
        default=os.environ.get("GOZIK_YTM_REGISTER_DESKTOP", "auto"),
        help=(
            "Register a desktop entry (app-menu shortcut) for the web UI. "
            "'auto' = register once if missing, 'always' = overwrite, "
            "'never' = skip. (default: auto)"
        ),
    )
    return parser.parse_args()


def serve(
    host: str,
    port: int,
    workers: int,
    webui_port: int,
    webui_register: str = "auto",
) -> None:
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

    # Start the optional web UI in a background thread.
    webui_server = None
    if webui_port > 0:
        from handlers.webui import start_webui
        webui_server = start_webui(servicer, webui_port)

        # Register a desktop entry so the user can launch the web UI from the
        # app menu without remembering a CLI command or port number.
        if webui_register != "never":
            try:
                from handlers.desktop_entry import register
                register(webui_port=webui_port, force=(webui_register == "always"))
            except Exception as exc:
                logger.warning("Desktop entry registration failed: %s", exc)

    # Bind to loopback only — this daemon is never exposed externally.
    server.add_insecure_port(bind_address)
    server.start()
    logger.info("gRPC server listening on %s", bind_address)

    # Start the GUI informational window in a background thread.
    if webui_port > 0:
        import threading
        gui_thread = threading.Thread(target=_show_gui_popup, args=(webui_port,), daemon=True)
        gui_thread.start()

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
        if webui_server is not None:
            logger.info("Shutting down web UI server")
            webui_server.shutdown()
        server.stop(GRACEFUL_SHUTDOWN_TIMEOUT_S)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Block the main thread.
    try:
        while not _stop_event["triggered"]:
            time.sleep(1)
    finally:
        logger.info("Server shutdown complete")


def _show_gui_popup(webui_port: int) -> None:
    """Show a simple tkinter GUI window to inform the user that the plugin is running,
    provide a button to open the web console, and warn them to close their browser first
    if they use the Auto Import feature."""
    # Skip when running as a PyInstaller bundle (tkinter is excluded at build time)
    # or without a graphical display. systemd user services CAN show GUI as long as
    # DISPLAY / WAYLAND_DISPLAY is present (set via PassEnvironment or
    # systemctl --user import-environment).
    if getattr(sys, "frozen", False):
        return
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return

    try:
        import tkinter as tk
        import webbrowser
        
        root = tk.Tk()
        root.title("gozik YouTube Music")
        
        # Center the window on screen
        window_width = 450
        window_height = 240
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        pos_x = int((screen_width - window_width) / 2)
        pos_y = int((screen_height - window_height) / 2)
        root.geometry(f"{window_width}x{window_height}+{pos_x}+{pos_y}")
        root.resizable(False, False)
        root.configure(bg="#0f0f0f")
        
        # Title label
        title_lbl = tk.Label(
            root,
            text="🎵 gozik YouTube Music Plugin",
            font=("Arial", 14, "bold"),
            fg="#ff0033",
            bg="#0f0f0f"
        )
        title_lbl.pack(pady=15)
        
        # Description
        info_lbl = tk.Label(
            root,
            text=f"The plugin server is running on port {webui_port}.\nYou can manage authentication via the Web UI.",
            font=("Arial", 10),
            fg="#e8e8e8",
            bg="#0f0f0f",
            justify="center"
        )
        info_lbl.pack(pady=5)
        
        # Critical warning/note for browser lock
        warn_lbl = tk.Label(
            root,
            text="⚠️ IMPORTANT: If you use 'Auto Import from Browser',\nplease CLOSE the browser first to prevent database lock errors.",
            font=("Arial", 9, "bold"),
            fg="#ffb300",
            bg="#0f0f0f",
            justify="center"
        )
        warn_lbl.pack(pady=10)
        
        # Buttons
        btn_frame = tk.Frame(root, bg="#0f0f0f")
        btn_frame.pack(pady=10)
        
        def open_url():
            webbrowser.open(f"http://127.0.0.1:{webui_port}")
            
        open_btn = tk.Button(
            btn_frame,
            text="Open Web UI",
            command=open_url,
            font=("Arial", 9, "bold"),
            fg="#ffffff",
            bg="#ff0033",
            activebackground="#cc0029",
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=4
        )
        open_btn.pack(side=tk.LEFT, padx=10)
        
        close_btn = tk.Button(
            btn_frame,
            text="Dismiss",
            command=root.destroy,
            font=("Arial", 9),
            fg="#ffffff",
            bg="#333333",
            activebackground="#444444",
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=4
        )
        close_btn.pack(side=tk.LEFT, padx=10)
        
        # Lift window to top once
        root.attributes("-topmost", True)
        root.update()
        root.attributes("-topmost", False)
        
        root.mainloop()
    except Exception as exc:
        logger.warning("Failed to start Tkinter GUI popup: %s", exc)


def main() -> None:
    """Entry point."""
    args = _parse_args()
    serve(
        host=args.host,
        port=args.port,
        workers=args.workers,
        webui_port=args.web_ui_port,
        webui_register=args.register_desktop_entry,
    )


if __name__ == "__main__":
    main()
