"""Measure bounded HTTP queries against an already running local workbench; no browser timing."""

from __future__ import annotations

import argparse
import http.client
import json
import time
from pathlib import Path
from urllib.parse import quote

import numpy as np


def request(port: int, path: str) -> tuple[int, dict, int, float]:
    started = time.perf_counter_ns()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read()
        body = json.loads(raw)
        elapsed_ms = (time.perf_counter_ns() - started) / 1e6
        return response.status, body, len(raw), elapsed_ms
    finally:
        connection.close()


def benchmark(port: int, run_id: str, repeats: int) -> dict:
    if repeats < 2:
        raise ValueError("at least two requests per scenario are required")
    base = "/runs/" + quote(run_id, safe="")
    code, manifest, _, _ = request(port, base + "/manifest")
    if code != 200:
        raise ValueError("run manifest is unavailable")
    end = manifest["steps"]
    scenarios = [
        ("adc_overview", "adc.code", end, 31),
        ("analogue_overview", "pmt.power_w", end, 31),
        ("adc_detail", "adc.code", min(end, 16), 800),
    ]
    results = {}
    for name, signal, stop, width in scenarios:
        timings, sizes, rows = [], [], []
        for index in range(repeats):
            path = (
                f"{base}/traces?signal={signal}&start=0&end={stop}&pixel_width={width}&req={index}"
            )
            code, body, size, elapsed = request(port, path)
            if code != 200 or body.get("status") not in {"exact_record", "summary_only"}:
                raise ValueError(f"query failed: {body}")
            row_count = len(body["values"])
            limit = width if body["status"] == "summary_only" else 2 * width
            if row_count > limit:
                raise ValueError("trace response exceeded its display budget")
            if signal == "adc.code" and body["source_count"] != -(-stop // manifest["os"]):
                raise ValueError("ADC query lost sample coverage")
            timings.append(elapsed)
            sizes.append(size)
            rows.append(row_count)
        results[name] = {
            "status": body["status"],
            "returned_rows": rows,
            "response_bytes": sizes,
            "latency_ms": timings,
            "first_request_ms": timings[0],
            "subsequent_p95_ms": float(np.percentile(timings[1:], 95)),
            "pixel_width": width,
            "source_count": body["source_count"],
        }
    return {
        "kind": "HTTP query payload and latency only",
        "run_id": run_id,
        "origin": manifest["origin"],
        "evidence": manifest["evidence"],
        "physical_applicability": manifest.get("physical_applicability"),
        "steps": end,
        "adc_samples": manifest["samples"],
        "repeats": repeats,
        "conditions": "live in-memory run; no response cache; OS disk cache not controlled",
        "excludes": [
            "simulation setup",
            "checkpoint replay",
            "browser rendering",
            "end-to-end UI latency",
        ],
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--run", default="demo")
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = benchmark(args.port, args.run, args.repeats)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=2) + "\n")
    print(f"HTTP benchmark written: {args.out}; no rendering or full UI latency claim")
