"""Read-only, bounded-size image queries and lineage from reconstruction artifacts."""

from __future__ import annotations

import json
import shutil
import tempfile
import zlib
from pathlib import Path
from zipfile import ZipFile

import numpy as np

from zlscan.contracts.calibration import Binding
from zlscan.contracts.flags import InvalidFlag
from zlscan.contracts.time import ClockDomain
from zlscan.io.manifest import sha256_file
from zlscan.trace.window import validate_window


class ImageArtifact:
    tile_size = 256

    def __init__(
        self,
        directory: Path,
        *,
        raw_path: Path | None = None,
        expected_calibration_id: str | None = None,
        expected_binding: dict | None = None,
    ) -> None:
        self.directory, self.raw_path = directory, raw_path
        self.run = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        if self.run.get("mode") != "reconstruct":
            raise ValueError("image service requires a reconstruction run")
        self.grid = self.run["grid"]
        self.width, self.height = self.grid["width"], self.grid["height"]
        self.mismatch = (
            expected_calibration_id is not None
            and expected_calibration_id != self.run["calibration_id"]
        )
        if expected_binding is not None:
            wanted = Binding.model_validate(expected_binding).model_dump(mode="json")
            self.mismatch |= wanted != self.run.get("capture_binding")
        self.expected_binding = expected_binding
        self.expected_calibration_id = expected_calibration_id
        paths = [directory / "run.json", directory / "density.npz"]
        if self.run.get("density_sha256") and sha256_file(paths[1]) != self.run["density_sha256"]:
            raise ValueError("image SHA-256 does not match reconstruction")
        self.plan = None
        if self.run.get("lineage_sha256"):
            plan_path = directory / "lineage.json"
            if sha256_file(plan_path) != self.run["lineage_sha256"]:
                raise ValueError("lineage SHA-256 does not match reconstruction")
            self.plan = json.loads(plan_path.read_text(encoding="utf-8"))
            if (
                self.plan.get("format") != "zlscan-image-lineage/1"
                or self.plan["raw_sha256"] != self.run["raw_sha256"]
                or self.plan["calibration_id"] != self.run["calibration_id"]
                or self.plan["grid"] != self.grid
            ):
                raise ValueError("lineage identity does not match reconstruction")
            paths.append(plan_path)
        if raw_path is not None:
            if sha256_file(raw_path) != self.run["raw_sha256"]:
                raise ValueError("raw SHA-256 does not match reconstruction")
            paths.append(raw_path)
        self._identities = {p: self._identity(p) for p in paths}
        self.clocks = (
            {c["id"]: ClockDomain(**c) for c in self.plan["clock_domains"]} if self.plan else {}
        )
        self.lines = self.plan["lines"] if self.plan else []
        self.delivered = [row for row in self.lines if row["source"] is not None]
        self.first_sample = min(
            (int(r["source"]["first_sample_index"]) for r in self.delivered), default=0
        )
        last = max(
            (
                int(r["source"]["first_sample_index"]) + r["source"]["sample_count"]
                for r in self.delivered
            ),
            default=0,
        )
        self.span = last - self.first_sample
        identities = {(r["source"]["epoch"], r["source"]["clock_domain"]) for r in self.delivered}
        self.timeline_available = len(identities) == 1 and bool(self.clocks)
        self.epoch, self.domain = next(iter(identities), (None, None))
        self.mappings = (
            {
                key: np.asarray([np.nan if x is None else x for x in values])
                for key, values in self.plan["mappings"].items()
            }
            if self.plan
            else {}
        )
        self._scratch = tempfile.TemporaryDirectory(prefix="zlscan_image_view_")
        self.arrays = {}
        try:
            with ZipFile(directory / "density.npz") as archive:
                for name in ("density", "flags", "support_weight"):
                    target = Path(self._scratch.name) / f"{name}.npy"
                    with archive.open(f"{name}.npy") as source, target.open("wb") as out:
                        shutil.copyfileobj(source, out, length=1 << 20)
                    value = np.load(target, mmap_mode="r", allow_pickle=False)
                    if value.shape != (self.height, self.width) or value.dtype.kind not in "iuf":
                        raise ValueError("image arrays do not match the reconstruction grid")
                    self.arrays[name] = value
        except Exception:
            self.close()
            raise

    @staticmethod
    def _identity(path: Path) -> tuple:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _unchanged(self) -> None:
        if any(self._identity(path) != stamp for path, stamp in self._identities.items()):
            raise ValueError("registered image or capture source changed; reload the registration")

    def close(self) -> None:
        for value in self.arrays.values():
            value._mmap.close()
        self.arrays.clear()
        self._scratch.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def manifest(self) -> dict:
        self._unchanged()
        return {
            "status": "exact_record",
            "image_id": "density",
            "width": self.width,
            "height": self.height,
            "tile_size": self.tile_size,
            "grid": self.grid,
            "calibration_id": self.run["calibration_id"],
            "calibration_provenance": self.run["calibration_provenance"],
            "calibration_status": "CALIBRATION_MISMATCH"
            if self.mismatch
            else "compatible_at_reconstruction",
            "expected_calibration_id": self.expected_calibration_id,
            "expected_binding": self.expected_binding,
            "capture_binding": self.run.get("capture_binding"),
            "raw_sha256": self.run["raw_sha256"],
            "density_sha256": self.run.get("density_sha256"),
            "lineage_sha256": self.run.get("lineage_sha256"),
            "valid_fraction": self.run["valid_fraction"],
            "unit": "OD",
            "lineage_status": "exact_record" if self.plan else "not_recorded",
            "raw_trace_status": "exact_record" if self.raw_path and self.plan else "not_recorded",
            "reasons": {str(int(flag)): flag.name for flag in InvalidFlag},
        }

    def tile(self, tile_x: int, tile_y: int) -> dict:
        self._unchanged()
        x, y = tile_x * self.tile_size, tile_y * self.tile_size
        if min(tile_x, tile_y) < 0 or x >= self.width or y >= self.height:
            raise ValueError("image tile lies outside the image")
        slices = np.s_[
            y : min(self.height, y + self.tile_size), x : min(self.width, x + self.tile_size)
        ]
        density = np.array(self.arrays["density"][slices])
        flags = np.array(self.arrays["flags"][slices])
        if self.mismatch:
            density[:] = np.nan
            flags |= int(InvalidFlag.CALIBRATION_MISMATCH)
        return {
            "status": "exact_record",
            "x": x,
            "y": y,
            "width": density.shape[1],
            "height": density.shape[0],
            "density": [[float(v) if np.isfinite(v) else None for v in row] for row in density],
            "flags": flags.tolist(),
            "support_weight": self.arrays["support_weight"][slices].tolist(),
            "calibration_status": self.manifest()["calibration_status"],
        }

    def pixel_lineage(self, row: int, col: int) -> dict:
        self._unchanged()
        if not (0 <= row < self.height and 0 <= col < self.width):
            raise ValueError("pixel lies outside the image")
        if self.mismatch or self.plan is None:
            return {
                "status": "not_computed" if self.mismatch else "not_recorded",
                "reason": "CALIBRATION_MISMATCH" if self.mismatch else "lineage was not recorded",
            }
        flags = int(self.arrays["flags"][row, col])
        support = float(self.arrays["support_weight"][row, col])
        samples, missing = [], []
        g = self.grid
        for line in self.lines:
            v = (line["y_m"] - g["origin_y_m"]) / g["pitch_y_m"] - 0.5
            wy = 1 - abs(row - v)
            if wy <= 0:
                continue
            x = self.mappings[line["mapping"]]
            u = (x - g["origin_x_m"]) / g["pitch_x_m"] - 0.5
            weights = (1 - np.abs(col - u)) * wy
            used = np.flatnonzero(np.isfinite(weights) & (weights > 0))
            source = line["source"]
            if source is None:
                if len(used):
                    missing.append(line["line"])
                continue
            clock = self.clocks[source["clock_domain"]]
            for index in used[used < source["sample_count"]]:
                sample = int(source["first_sample_index"]) + int(index)
                weight = float(weights[index])
                samples.append(
                    {
                        "session_id": self.plan["session_id"],
                        "epoch": source["epoch"],
                        "clock_domain": source["clock_domain"],
                        "channel": self.plan["channel"],
                        "sample_index": str(sample),
                        "sample_time_ticks": str(clock.tick_of(sample)),
                        "line": line["line"],
                        "column": int(index),
                        "weight": weight,
                        "normalized_weight": weight / support
                        if flags == 0 and support > 0
                        else None,
                    }
                )
        times = [int(s["sample_time_ticks"]) for s in samples]
        return {
            "status": "exact_record",
            "row": row,
            "column": col,
            "flags": flags,
            "samples": samples,
            "missing_lines": missing,
            "sample_time_range_ticks": [str(min(times)), str(max(times))] if times else None,
            "meaning": "linear-domain reconstruction weights"
            if flags == 0
            else "geometric support candidates; invalid pixel",
        }

    def state(self, offset: int) -> dict:
        self._unchanged()
        if not self.timeline_available:
            return {
                "status": "not_computed",
                "reason": "select one epoch and clock domain for a timeline",
            }
        if not 0 <= offset < self.span:
            raise ValueError("cursor lies outside the capture")
        sample = self.first_sample + offset
        row = next(
            (
                r["line"]
                for r in self.delivered
                if int(r["source"]["first_sample_index"])
                <= sample
                < int(r["source"]["first_sample_index"]) + r["source"]["sample_count"]
            ),
            None,
        )
        return {
            "status": "exact_record",
            "sample_index": str(sample),
            "sample_time_ticks": str(self.clocks[self.domain].tick_of(sample)),
            "step": offset,
            "current_line": row,
            "epoch": self.epoch,
            "values": {},
            "reason": "recorded capture; internal physical state not registered",
        }

    def traces(self, start: int, end: int, pixel_width: int = 800) -> dict:
        self._unchanged()
        validate_window(start, end, pixel_width)
        if self.raw_path is None or not self.plan or not self.timeline_available:
            return {
                "status": "not_recorded",
                "reason": "matching raw capture and one clock domain required",
            }
        requested = [start, end]
        start, end = max(0, start), min(self.span, end)
        if end <= start:
            return {"status": "not_computed", "reason": "window is outside the capture"}
        count = end - start
        block = 1 if count <= 2 * pixel_width else -(-count // pixel_width)
        size = -(-count // block)
        # Storage follows the display budget, including gaps; never allocate the source span.
        lows, highs, firsts, lasts = ([None] * size for _ in range(4))
        recorded = [0] * size
        with self.raw_path.open("rb") as stream:
            for row in self.delivered:
                source = row["source"]
                first = int(source["first_sample_index"]) - self.first_sample
                lo, hi = max(start, first), min(end, first + source["sample_count"])
                if hi <= lo:
                    continue
                stream.seek(source["payload_offset"])
                payload = stream.read(source["sample_count"] * len(source["channels"]) * 2)
                if zlib.crc32(payload) != source["payload_crc32"]:
                    raise ValueError("raw payload changed or its CRC is invalid")
                codes = np.frombuffer(payload, dtype="<u2").reshape(-1, len(source["channels"]))
                codes = codes[:, source["channels"].index(self.plan["channel"])]
                for bucket in range((lo - start) // block, -(-(hi - start) // block)):
                    edge = start + bucket * block
                    a, b = max(lo, edge), min(hi, edge + block)
                    chunk = codes[a - first : b - first]
                    low, high = int(chunk.min()), int(chunk.max())
                    lows[bucket] = low if lows[bucket] is None else min(lows[bucket], low)
                    highs[bucket] = high if highs[bucket] is None else max(highs[bucket], high)
                    if a == edge:
                        firsts[bucket] = int(chunk[0])
                    if b == min(end, edge + block):
                        lasts[bucket] = int(chunk[-1])
                    recorded[bucket] += b - a
        self._unchanged()
        clock = self.clocks[self.domain]
        return {
            "status": "exact_record" if block == 1 else "summary_only",
            "signal": "adc.code",
            "level": "raw" if block == 1 else "display_bins",
            "start": start,
            "first_step": start,
            "block_size": block,
            "block_steps": block,
            "source_count": count,
            "support_start": start,
            "support_end": end,
            "boundary_extra": False,
            "requested_window": requested,
            "unit_step": "sample",
            "sample_origin": str(self.first_sample),
            "display_interpolation": "hold",
            "values": firsts
            if block == 1
            else [
                [lows[i], highs[i], firsts[i], lasts[i], min(block, count - i * block)]
                for i in range(size)
            ],
            "columns": ["min", "max", "first", "last", "count"],
            "recorded_counts": recorded,
            "missing_count": count - sum(recorded),
            "count_semantics": "sample positions, including missing; recorded_counts exclude gaps",
            "sample_time": {
                "clock_domain": self.domain,
                "first_sample_index": str(self.first_sample + start),
                "first_tick_ps": str(clock.tick_of(self.first_sample + start)),
                "last_tick_ps": str(clock.tick_of(self.first_sample + end - 1)),
                "period_ps": {
                    "numerator": str(10**12 * clock.freq_den),
                    "denominator": str(clock.freq_num),
                },
            },
        }
