"""Build an offline density and quality-mask workbench for reconstructed captures."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from zlscan.contracts.flags import InvalidFlag
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import read_all

_TEMPLATE = Path(__file__).with_name("image_view.html")


def _image_payload(directory: Path, raw_path: Path | None) -> dict:
    run = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    if run.get("mode") != "reconstruct":
        raise ValueError("image workbench requires a reconstruction run")
    with np.load(directory / "density.npz") as arrays:
        density = arrays["density"]
        flags = arrays["flags"]
        support = arrays["support_weight"]
    grid = run["grid"]
    if density.shape != (grid["height"], grid["width"]) or flags.shape != density.shape:
        raise ValueError("image shape does not match its grid")
    lineage = None
    if raw_path is not None and not run.get("facet_timing_sha256"):
        if sha256_file(raw_path) != run["raw_sha256"]:
            raise ValueError("lineage capture does not match the reconstruction SHA-256")
        raw = read_all(raw_path)
        if raw.fatal or raw.session is None:
            raise ValueError("lineage requires a readable ZLRAW capture")
        clocks = {item["id"]: item for item in raw.session.clock_domains}
        lineage = {
            "window": raw.session.windows["window_length"],
            "delay_offset_samples": run.get("delay_offset_samples", 0),
            "lines": {
                str(block.line_number): {
                    "first": str(block.first_sample_index),
                    "count": block.sample_count,
                    "clock": clocks.get(block.clock_domain),
                }
                for block in raw.blocks
            },
        }
    return {
        "label": directory.name,
        "calibration_id": run["calibration_id"],
        "provenance": run["calibration_provenance"],
        "valid_fraction": run["valid_fraction"],
        "width": grid["width"],
        "height": grid["height"],
        "density": [float(value) if np.isfinite(value) else None for value in density.ravel()],
        "flags": flags.ravel().tolist(),
        "support": support.ravel().tolist(),
        "lineage": lineage,
    }


def write_image_view(
    directory: Path,
    output: Path,
    *,
    raw_path: Path | None = None,
    comparison: Path | None = None,
    comparison_raw: Path | None = None,
) -> None:
    """Write an offline workbench with optional synchronized comparison."""
    images = [_image_payload(directory, raw_path)]
    if comparison is not None:
        images.append(_image_payload(comparison, comparison_raw))
    data = {
        "images": images,
        "reasons": {int(flag): flag.name for flag in InvalidFlag},
    }
    # JSON inside a script element must not introduce an HTML closing tag.
    payload = json.dumps(data, allow_nan=False).replace("<", "\\u003c")
    html = _TEMPLATE.read_text(encoding="utf-8").replace("__ZLSCAN_IMAGE_DATA__", payload, 1)
    with output.open("x", encoding="utf-8") as file:
        file.write(html)
