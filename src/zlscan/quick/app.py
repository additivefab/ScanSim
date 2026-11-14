"""Small writable HTTP app for the Quick MVP."""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from .model import QuickConfig, run_quick


class QuickServer(ThreadingHTTPServer):
    def __init__(self, address, root: Path):
        self.root = root.resolve()
        self.latest: Path | None = None
        super().__init__(address, QuickHandler)


class QuickHandler(BaseHTTPRequestHandler):
    server: QuickServer

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(self.path.split("?", 1)[0])
        if path == "/api/current":
            if not self.server.latest:
                self._json(404, {"error": "no quick run yet"})
                return
            metadata = json.loads(
                (self.server.latest / "quick_run.json").read_text(encoding="utf-8")
            )
            self._json(
                200,
                {
                    "run": str(self.server.latest.relative_to(self.server.root)),
                    "metadata": metadata,
                },
            )
            return
        relative = "quick.html" if path == "/" else path.lstrip("/")
        target = (self.server.root / relative).resolve()
        if not target.is_file() or not target.is_relative_to(self.server.root):
            self.send_error(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/run":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            cfg = QuickConfig.from_mapping(data)
            run_id = time.strftime("run-%Y%m%d-%H%M%S") + f"-{threading.get_ident()}"
            result = run_quick(cfg, self.server.root / "runs" / run_id)
            self.server.latest = result.run_dir
            self._json(
                200,
                {
                    "run": str(result.run_dir.relative_to(self.server.root)),
                    "metadata": result.metadata,
                },
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, format: str, *args) -> None:
        return


def serve_quick(root: Path, port: int = 8766) -> None:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    server = QuickServer(("127.0.0.1", port), root)
    print(f"Quick MVP：http://127.0.0.1:{port}/  （Ctrl+C 结束）", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
