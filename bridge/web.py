"""Browser control surface for ek-bridge (same approach as the MiniVP Middleware UI).

Standard library only: a ThreadingHTTPServer on a daemon thread serves one inline page
(bridge/ui/index.html - no CDN, no build step, works offline on a stage machine) and a
small JSON API. Requests hop onto the bridge's asyncio loop with run_coroutine_threadsafe,
so the Bluetooth and Unreal links are only ever touched from the loop.

Binding defaults to loopback. There is no authentication and this moves a lens, so
exposing it to the network is an explicit --web-bind choice.

  GET  /                 the page
  GET  /api/state        snapshot (polled ~10 Hz by the page)
  POST /api/<action>     JSON body -> Bridge.act_<action>(**body); returns {ok, result, state} or {error}
"""
from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MAX_BODY_BYTES = 64 * 1024
PAGE_PATH = Path(__file__).with_name("ui") / "index.html"


class PortInUse(RuntimeError):
    """The UI port is already taken, most often by another bridge instance."""


def _send(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200, ctype: str = "application/json") -> None:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(bridge, loop: asyncio.AbstractEventLoop):
    def on_loop(coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    class Handler(BaseHTTPRequestHandler):
        server_version = "EkBridge/1.0"

        def log_message(self, fmt, *args):  # the page polls at 10 Hz; keep the terminal readable
            pass

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                raise ValueError("request body too large")
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("JSON object expected")
            return data

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                _send(self, PAGE_PATH.read_bytes(), ctype="text/html; charset=utf-8")
            elif path == "/api/state":
                try:
                    _send(self, on_loop(bridge.snapshot(), 2.0))
                except FutureTimeout:
                    _send(self, {"error": "bridge busy"}, 503)
            else:
                self.send_error(404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not path.startswith("/api/"):
                self.send_error(404)
                return
            # Simple CSRF guard: only accept JSON posts (a cross-site form can't set this header).
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                _send(self, {"error": "Content-Type must be application/json"}, 415)
                return
            action = path[len("/api/"):]
            try:
                result = on_loop(bridge.ui_action(action, self._read_json()), 15.0)
                state = on_loop(bridge.snapshot(), 2.0)
                _send(self, {"ok": True, "result": result, "state": state})
            except KeyError as e:
                _send(self, {"error": str(e).strip("'\"")}, 404 if "unknown action" in str(e) else 400)
            except (ValueError, TypeError, RuntimeError, FileNotFoundError, FileExistsError, ConnectionError) as e:
                _send(self, {"error": str(e)}, 400)
            except FutureTimeout:
                _send(self, {"error": "timed out waiting for the bridge"}, 504)
            except Exception as e:  # keep the server alive on surprises
                _send(self, {"error": repr(e)}, 500)

    return Handler


def start(bridge, loop: asyncio.AbstractEventLoop, host: str = "127.0.0.1", port: int = 8766) -> ThreadingHTTPServer:
    try:
        server = ThreadingHTTPServer((host, port), make_handler(bridge, loop))
    except OSError as e:
        raise PortInUse(f"cannot serve the web UI on {host}:{port}: {e} (another bridge running? use --web-port)") from e
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="web-ui").start()
    return server
