"""Small writable HTTP app for the Quick MVP."""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

from .model import QuickConfig, run_quick


class QuickServer(ThreadingHTTPServer):
    def __init__(self, address, root: Path):
        self.root = root.resolve()
        self.latest: Path | None = None
        self.previous: Path | None = None
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
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path == "/api/row":
            try:
                run = (self.server.root / query["run"][0]).resolve()
                row = int(query["row"][0])
                if not run.is_relative_to(self.server.root) or not (run / "arrays.npz").is_file():
                    raise ValueError("unknown quick run")
                with np.load(run / "arrays.npz") as arrays:
                    if row < 0 or row >= arrays["voltage_v"].shape[0]:
                        raise ValueError("row outside quick run")
                    fields = {
                        name: arrays[name][row]
                        for name in (
                            "time_s",
                            "x_m",
                            "y_m",
                            "power_rel",
                            "current_rel",
                            "voltage_v",
                            "code",
                            "density_hat",
                            "valid",
                            "saturated",
                        )
                    }
                payload = {
                    "row": row,
                    "values": {
                        key: [self._json_value(value) for value in values]
                        for key, values in fields.items()
                    },
                }
                self._json(200, payload)
            except (KeyError, OSError, ValueError, TypeError) as exc:
                self._json(400, {"error": str(exc)})
            return
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
                    "previous": self._run_payload(self.server.previous),
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
            upload = data.pop("input_data", None)
            if upload:
                name = Path(str(data.pop("input_name", "upload.png"))).name
                suffix = Path(name).suffix.lower()
                if suffix not in {".png", ".tif", ".tiff"}:
                    raise ValueError("uploaded input must be PNG or TIFF")
                upload_dir = self.server.root / "uploads"
                upload_dir.mkdir(parents=True, exist_ok=True)
                upload_path = upload_dir / f"input-{time.strftime('%Y%m%d-%H%M%S')}{suffix}"
                upload_path.write_bytes(base64.b64decode(str(upload), validate=True))
                data["input_path"] = str(upload_path)
            cfg = QuickConfig.from_mapping(data)
            run_id = time.strftime("run-%Y%m%d-%H%M%S") + (
                f"-{time.time_ns()}-{threading.get_ident()}"
            )
            result = run_quick(cfg, self.server.root / "runs" / run_id)
            self.server.previous = self.server.latest
            self.server.latest = result.run_dir
            self._json(
                200,
                {
                    "run": str(result.run_dir.relative_to(self.server.root)),
                    "metadata": result.metadata,
                    "previous": self._run_payload(self.server.previous),
                },
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
            self._json(400, {"error": str(exc)})

    def _run_payload(self, run: Path | None) -> dict | None:
        if not run or not run.is_dir():
            return None
        metadata = json.loads((run / "quick_run.json").read_text(encoding="utf-8"))
        return {"run": str(run.relative_to(self.server.root)), "metadata": metadata}

    @staticmethod
    def _json_value(value):
        if isinstance(value, (bool, np.bool_)):
            return bool(value)
        numeric = float(value)
        return numeric if np.isfinite(numeric) else None

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
