"""Launch the single-user local ThesisHelper app on Windows, macOS, or Linux."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"


@contextlib.contextmanager
def single_instance(root: Path):
    """An OS lock also protects the same workspace from a second port/server."""
    directory = root / "data"
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / ".server.lock").open("a+b")
    try:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise RuntimeError(
                "ThesisHelper is already running for this project folder. "
                "Use its existing browser tab, or stop that server before starting another."
            ) from exc
        yield
    finally:
        lock.close()


def _open_when_ready(port: int, stopped: threading.Event) -> None:
    url = f"http://{HOST}:{port}"
    # Loopback must never pass through an enterprise HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 30
    while not stopped.is_set() and time.monotonic() < deadline:
        try:
            with opener.open(f"{url}/api/health", timeout=1) as response:
                payload = json.load(response)
            if payload.get("ok") or payload.get("status") in {"ok", "healthy"}:
                webbrowser.open(url)
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        stopped.wait(0.3)
    if not stopped.is_set():
        print(f"Open {url} in your browser when startup finishes.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="ThesisHelper local Windows app")
    parser.add_argument("--port", default=os.getenv("THESISHELPER_PORT", "8765"),
                        help="Loopback port (default: 8765 or THESISHELPER_PORT)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    args = parser.parse_args()
    try:
        port = int(args.port)
    except (ValueError, TypeError):
        parser.error("The port must be a number between 1024 and 65535.")
    if not 1024 <= port <= 65535:
        parser.error("The port must be between 1024 and 65535.")
    if not (3, 11) <= sys.version_info[:2] < (3, 14):
        print("Use 64-bit Python 3.11, 3.12, or 3.13. Python 3.12 is recommended.", file=sys.stderr)
        return 1
    try:
        import struct
        if struct.calcsize("P") != 8:
            raise RuntimeError("Install 64-bit Python to use ThesisHelper.")
        import uvicorn
        from thesishelper.app import create_app
    except ImportError as exc:
        print(f"Missing dependency: {exc.name}. Run install.bat first.", file=sys.stderr)
        return 1
    stopped = threading.Event()
    try:
        with single_instance(ROOT):
            # Keep this bound socket for uvicorn to eliminate the port-check race.
            server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if os.name == "nt":
                    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                server_socket.bind((HOST, port))
                server_socket.listen(128)
                server_socket.setblocking(False)
            except OSError as exc:
                server_socket.close()
                raise RuntimeError(
                    f"Port {port} is already in use or unavailable. Stop the other application, "
                    f"or run start.bat --port {port + 1 if port < 65535 else 8765}."
                ) from exc
            try:
                app = create_app(ROOT)
                config = uvicorn.Config(app, host=HOST, port=port, log_level="info", access_log=False)
                server = uvicorn.Server(config)
                print(f"\nThesisHelper: http://{HOST}:{port}\n"
                      "This server is accessible only from this computer.\n"
                      "Keep this window open. Press Ctrl+C to stop.\n", flush=True)
                if not args.no_browser:
                    threading.Thread(target=_open_when_ready, args=(port, stopped), daemon=True).start()
                server.run(sockets=[server_socket])
            finally:
                stopped.set()
                server_socket.close()
    except KeyboardInterrupt:
        return 0
    except (RuntimeError, OSError) as exc:
        print(f"\nCould not start ThesisHelper: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
