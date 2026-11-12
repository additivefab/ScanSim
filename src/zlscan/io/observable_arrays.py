"""Canonical compact storage for the digital recorder's observable vectors only."""

from __future__ import annotations

import base64
import json
import zlib

import numpy as np

INTEGER_KEYS = {"capture_time", "capture_count", "selection_time", "window_count"}
FLAG_KEYS = {"overflow", "overflow_valid"}


def encode_arrays(arrays: dict[str, np.ndarray]) -> dict:
    result = {}
    for key, value in sorted(arrays.items()):
        if key not in INTEGER_KEYS | FLAG_KEYS or value.ndim != 1:
            raise ValueError("unsupported observable array")
        if key in FLAG_KEYS:
            kind, data = "bits", np.packbits(value.astype(bool), bitorder="little").tobytes()
        else:
            values = [int(v) for v in value]
            if all(-(2**63) <= v < 2**63 for v in values):
                kind, data = "i8", np.asarray(values, dtype="<i8").tobytes()
            else:
                kind, data = (
                    "decimal",
                    json.dumps([str(v) for v in values], separators=(",", ":")).encode(),
                )
        result[key] = {
            "kind": kind,
            "count": len(value),
            "data": base64.b64encode(zlib.compress(data, level=1)).decode("ascii"),
        }
    return result


def decode_arrays(encoded: dict) -> dict[str, np.ndarray]:
    result = {}
    for key, value in encoded.items():
        if (
            key not in INTEGER_KEYS | FLAG_KEYS
            or type(value["count"]) is not int
            or value["count"] < 0
        ):
            raise ValueError("invalid observable array identity or count")
        raw = zlib.decompress(base64.b64decode(value["data"], validate=True))
        count = value["count"]
        if value["kind"] == "bits" and key in FLAG_KEYS and len(raw) == -(-count // 8):
            result[key] = np.unpackbits(np.frombuffer(raw, np.uint8), bitorder="little")[
                :count
            ].astype(bool)
        elif value["kind"] == "i8" and key in INTEGER_KEYS and len(raw) == count * 8:
            result[key] = np.frombuffer(raw, dtype="<i8").copy()
        elif value["kind"] == "decimal" and key in INTEGER_KEYS:
            values = json.loads(raw)
            if len(values) != count:
                raise ValueError("observable decimal count differs")
            result[key] = np.asarray([int(v) for v in values], object)
        else:
            raise ValueError("observable array layout differs")
    return result
