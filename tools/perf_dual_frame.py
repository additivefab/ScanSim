"""Run the two S3-T11 research frames through physical capture, transport and verified disk IO."""

from __future__ import annotations

import argparse
import json
import resource
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from zlscan.config.presets import nominal_config
from zlscan.engine.chain import ChainConfig, Phase
from zlscan.engine.stream_physical import stream_physical
from zlscan.evidence.records import compare, environment_info, git_state, write_record
from zlscan.io.manifest import sha256_file
from zlscan.truth.psf import PsfConfig
from zlscan.truth.scene import step_wedge, uniform

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "4000x5000": (4000, 5000, 50_000_000, 5),
    "7000x8600": (7000, 8600, 20_000_000, 2),
}
REFERENCE_ROWS = 4
PITCH_M = 50e-6


def configurations(
    width: int,
    height: int,
    sample_rate: int,
    spp: int,
) -> tuple[ChainConfig, ChainConfig]:
    """Use a deterministic research scene with two independent optical inputs."""
    a = nominal_config()
    a.fs_hz = sample_rate
    a.os = 4
    velocity = PITCH_M * sample_rate / spp
    a.scan = replace(a.scan, facets=6, rpm=10_000)
    a.scan = replace(
        a.scan,
        focal_m=velocity / (2 * a.scan.omega),
        pitch_x_m=PITCH_M,
        pitch_y_m=PITCH_M,
        nx=width,
        x_center_m=width * PITCH_M / 2,
        active_offset_s=0.1e-3,
    )
    a.detector = replace(a.detector, shot_noise=False)
    a.psf = PsfConfig(45e-6, 45e-6, nodes_x=3, nodes_y=3)
    a.source = replace(a.source, gated=False)
    width_m = a.scan.width_m
    height_m = (height + 2 * REFERENCE_ROWS) * PITCH_M
    white = uniform(0, width_m, height_m)
    a.scenes = {"white": white, "film": step_wedge([0.25, 0.8, 1.5, 2.1], width_m, height_m)}
    a.timeline = [
        Phase("dark", 0, REFERENCE_ROWS, "white", laser_on=False),
        Phase("white", REFERENCE_ROWS, REFERENCE_ROWS, "white"),
        Phase("sample", 2 * REFERENCE_ROWS, height, "film"),
    ]
    b = replace(
        a,
        seed=2,
        scenes={"white": white, "film": step_wedge([1.9, 0.6, 1.2, 0.35], width_m, height_m)},
    )
    return a, b


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("case", choices=sorted(CASES))
    p.add_argument("out", type=Path)
    p.add_argument("--sample-lines", type=int, help="short pilot only")
    args = p.parse_args()
    nx, ny, fs, spp = CASES[args.case]
    height = ny if args.sample_lines is None else args.sample_lines
    if not 0 < height <= ny:
        p.error("sample line count outside the case dimensions")
    start = datetime.now(UTC)
    code = git_state(ROOT)
    a, b = configurations(nx, height, fs, spp)
    total_lines = height + 2 * REFERENCE_ROWS
    print(f"{args.case}: {height} sample rows + {2 * REFERENCE_ROWS} reference rows", flush=True)
    run = stream_physical(args.out / "output", a, b, total_lines)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    run.update(
        case=args.case,
        width_pixels=nx,
        height_pixels=height,
        reference_rows=2 * REFERENCE_ROWS,
        sampling_hz=fs,
        samples_per_pixel=spp,
        off_film_illumination=True,
        shot_noise=False,
        spot_quadrature_nodes=[3, 3],
        peak_rss_bytes=peak if sys.platform == "darwin" else peak * 1024,
        assumed_parameters=True,
    )
    criteria = [
        compare("integrity", "complete", run["integrity"]),
        compare("all rows retained", total_lines, run["written_blocks"]),
        compare("both channels retained", 2, len(run["channels"])),
        compare(
            "retained samples per channel",
            total_lines * nx * spp,
            run["retained_samples_per_channel"],
        ),
    ]
    passed = all(item["passed"] for item in criteria)
    summary = json.dumps(run, indent=2)
    (args.out / "output" / "performance.json").write_text(summary + "\n", encoding="utf-8")
    write_record(
        args.out / "record",
        name=f"dual_frame_{args.case}",
        status="measured",
        conclusion="pass" if passed else "fail",
        commands=[{"cmd": " ".join([sys.executable, *sys.argv]), "returncode": 0 if passed else 1}],
        inputs={
            "channel_0": a.hash(),
            "channel_1": b.hash(),
            "benchmark_script": sha256_file(Path(__file__)),
        },
        seed=[a.seed, b.seed],
        criteria=criteria,
        started_at=start,
        finished_at=datetime.now(UTC),
        log_text=summary,
        code=code,
        environment=environment_info(),
        notes="Deterministic research model. Peak RSS covers the entire command process.",
    )
    print(summary, flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
