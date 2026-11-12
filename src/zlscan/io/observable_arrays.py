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


def decode_sample(encoded: dict, index: int, expected_count: int) -> dict:
    """Decode one whitelisted sample with a bounded inflater; never expose diagnostics."""
    if type(expected_count) is not int or not 0 < expected_count <= 1_000_000:
        raise ValueError("observable packet exceeds per-query sample budget")
    if type(index) is not int or not 0 <= index < expected_count:
        raise ValueError("observable sample outside packet")
    if not isinstance(encoded, dict):
        raise ValueError("observable sample vectors must be an object")
    result = {}
    for key, value in encoded.items():
        if (
            key not in INTEGER_KEYS | FLAG_KEYS
            or not isinstance(value, dict)
            or value.get("count") != expected_count
            or type(value.get("count")) is not int
            or not isinstance(value.get("data"), str)
            or len(value["data"]) > 48 * (1 << 20)
        ):
            raise ValueError("invalid observable sample layout or input budget")
        kind = value["kind"]
        size = -(-expected_count // 8) if kind == "bits" else expected_count * 8
        budget = (32 << 20) if kind == "decimal" else size
        inflater = zlib.decompressobj()
        try:
            raw = inflater.decompress(base64.b64decode(value["data"], validate=True), budget + 1)
        except (ValueError, zlib.error) as exc:
            raise ValueError("invalid compressed observable sample") from exc
        if (
            len(raw) > budget
            or not inflater.eof
            or inflater.unused_data
            or inflater.unconsumed_tail
        ):
            raise ValueError("observable compressed value exceeds declared layout")
        if kind == "bits" and key in FLAG_KEYS and len(raw) == size:
            result[key] = bool((raw[index // 8] >> (index % 8)) & 1)
        elif kind == "i8" and key in INTEGER_KEYS and len(raw) == size:
            result[key] = int(np.frombuffer(raw, dtype="<i8", count=1, offset=index * 8)[0])
        elif kind == "decimal" and key in INTEGER_KEYS:
            values = json.loads(raw)
            if not isinstance(values, list) or len(values) != expected_count:
                raise ValueError("observable decimal count differs")
            result[key] = int(values[index])
        else:
            raise ValueError("observable sample encoding differs")
    return result
