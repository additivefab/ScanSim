"""Command-line entry point.

Every planned sub-command is declared here so that ``zlscan --help`` shows the whole
surface (see docs/dev/02_architecture.md, section 8). A command that is not implemented
yet must fail loudly: it prints a notice and exits with ``NOT_IMPLEMENTED_EXIT_CODE``
instead of succeeding silently.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from zlscan import __version__

USAGE_EXIT_CODE = 2
NOT_IMPLEMENTED_EXIT_CODE = 3
# zlscan check: 0 means the data is complete, which is not the same as verified hardware.
CHECK_FAIL_EXIT_CODE = 1
CHECK_UNRESOLVED_EXIT_CODE = 4


class Command:
    """A planned sub-command and the stage (docs/dev/stages) that first implements it."""

    def __init__(self, name: str, summary: str, stage: int) -> None:
        self.name = name
        self.summary = summary
        self.stage = stage


COMMANDS: tuple[Command, ...] = (
    Command("budget", "feasibility budget: geometry, photons, signal levels, data path", 1),
    Command("check", "rule checks on the hardware profile and configuration", 0),
    Command("run", "run a simulation and write a run directory", 2),
    Command("sweep", "parameter sweep over independent runs", 7),
    Command("calibrate", "estimate a calibration file from calibration scans", 4),
    Command("reconstruct", "reconstruct an image from a raw stream and a calibration file", 4),
    Command("metrics", "evaluate image quality against the simulation truth", 4),
    Command("ident", "import measurements and identify parameters", 6),
    Command(
        "compare", "compare two runs, simulation against measurement, or two implementations", 7
    ),
    Command("report", "build an offline report for a run", 1),
    Command("cosim", "co-simulate the RTL against the digital reference model", 2),
    Command("replay", "replay a time window from the nearest checkpoint", 3),
    Command("serve", "start the local query service for the workbench", 3),
)


def _run_check() -> int:
    """Rule checks on the hardware profile built from the unified parameter table."""
    from zlscan.evidence.table import load_parameters
    from zlscan.hardware.pins import PinRegistry
    from zlscan.hardware.profile import ProfileStatus, profile_gate

    try:
        registry = PinRegistry.load()
    except ValueError as exc:
        print(f"引脚表结构检查失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    gate = profile_gate(load_parameters())
    if gate.status is ProfileStatus.UNRESOLVED:
        print(f"硬件画像门槛：未决（缺少 {len(gate.missing)} 项资料）")
        for label in gate.missing:
            print(f"  - {label}")
    else:
        print("硬件画像门槛：资料齐全，可进入进一步验证")
    tables = "、".join(f"{rev} {conn}" for rev, conn in registry.tables())
    print(
        f"引脚表：{len(registry.rows)} 行（{tables}），结构检查通过。"
        "这是板端连接器对照，不是接线批准。"
    )
    print(gate.note)
    from zlscan.budget.runner import run_budget

    budget = run_budget()
    conflicts = [c for c in budget["conclusions"] if c["status"] == "FAIL"]
    unresolved = sum(1 for c in budget["conclusions"] if c["status"] == "UNRESOLVED")
    print(f"预算约束：{unresolved} 项无法计算，{len(conflicts)} 项矛盾。")
    for c in conflicts:
        print(f"  - {c['title']}: {c['text']}")
    if conflicts:
        return CHECK_FAIL_EXIT_CODE
    return (
        0
        if gate.status is ProfileStatus.READY_FOR_FURTHER_VALIDATION
        else CHECK_UNRESOLVED_EXIT_CODE
    )


def _run_budget(argv: Sequence[str]) -> int:
    import json
    from pathlib import Path

    from zlscan.budget.runner import BudgetRefused, run_budget, write_outputs

    p = argparse.ArgumentParser(prog="zlscan budget")
    p.add_argument("--config", type=Path, help="JSON config; omitted = hardware draft")
    p.add_argument("--mode", choices=["research", "hardware_constrained"])
    p.add_argument("--out", type=Path, default=Path("budget_out"))
    args = p.parse_args(list(argv))
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    if args.mode:
        config["mode"] = args.mode
    try:
        result = run_budget(config)
    except BudgetRefused as exc:
        print("hardware_constrained 拒绝运行：", file=sys.stderr)
        for line in exc.problems:
            print(f"  - {line}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    j, h = write_outputs(result, args.out)
    states = [c["status"] for c in result["conclusions"]]
    print(
        f"预算结论 {len(states)} 项："
        + "，".join(f"{s} {states.count(s)}" for s in sorted(set(states)))
    )
    print(f"已写入 {j} 与 {h}")
    if result["flagged_assumed_scenario"]:
        print("注意：这是假设场景，数字不是硬件事实。")
    if "FAIL" in states:
        return CHECK_FAIL_EXIT_CODE
    return CHECK_UNRESOLVED_EXIT_CODE if "UNRESOLVED" in states else 0


def _run_run(argv: Sequence[str]) -> int:
    from dataclasses import replace
    from pathlib import Path

    from zlscan.config.presets import demo_config, nominal_config
    from zlscan.engine.digital_run import run_digital

    p = argparse.ArgumentParser(prog="zlscan run")
    p.add_argument("--mode", choices=["digital", "physical", "calibration"], required=True)
    p.add_argument("--out", type=Path, default=Path("run_out"))
    p.add_argument("--lines", type=int, default=8)
    p.add_argument("--preset", choices=["demo", "nominal"], default="demo")
    p.add_argument("--stream", action="store_true", help="stream two physical ADC channels")
    args = p.parse_args(list(argv))
    if args.stream and args.mode != "physical":
        p.error("--stream requires physical mode")
    preset = nominal_config if args.preset == "nominal" else demo_config
    if args.mode == "calibration":
        from zlscan.experiment.calibration_scan import run_calibration_scan

        if args.lines < 2:
            p.error("calibration mode requires at least two lines per target")
        run = run_calibration_scan(args.out, preset(), lines_per_target=args.lines)
    elif args.mode == "physical":
        from zlscan.engine.physical_run import run_physical

        if args.lines < 1:
            p.error("--lines must be positive")
        if args.stream:
            from zlscan.engine.stream_physical import stream_physical

            primary = preset()
            secondary = replace(primary, seed=primary.seed + 1)
            run = stream_physical(args.out, primary, secondary, args.lines)
        else:
            run = run_physical(args.out, preset(), args.lines, save_checkpoints=True)
    else:
        run = run_digital(args.out)
    print(
        f"{run['mode']} 模式完成：重建 {run['recon_status']}，"
        f"完整性 {run['integrity']}，写入 {args.out}"
    )
    return 0 if run["recon_status"] == "complete" else CHECK_FAIL_EXIT_CODE


def _run_calibrate(argv: Sequence[str]) -> int:
    import json
    from pathlib import Path

    from zlscan.calib.file import save_calibration
    from zlscan.calib.observations import calibration_from_scan

    p = argparse.ArgumentParser(prog="zlscan calibrate")
    p.add_argument("--raw", required=True, type=Path)
    p.add_argument("--protocol", required=True, type=Path)
    p.add_argument("--timing", type=Path, help="synchronized injection observation JSON")
    p.add_argument("--quality", type=Path, help="observable quality event JSON")
    p.add_argument("--facets", type=Path, help="observable facet timing and calibration landmarks")
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args(list(argv))
    try:
        cal = calibration_from_scan(
            args.raw,
            args.protocol,
            fit_transfer=True,
            timing_path=args.timing,
            quality_path=args.quality,
            facet_path=args.facets,
        )
        args.out.mkdir(parents=True)
        save_calibration(args.out / "calibration.json", cal)
        summary = {
            "mode": "calibrate",
            "calibration_id": cal.calibration_id,
            "provenance": cal.provenance.value,
            "items": list(cal.items),
        }
        (args.out / "run.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"标定失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    print(f"标定文件已写入 {args.out}")
    return 0


def _run_reconstruct(argv: Sequence[str]) -> int:
    from pathlib import Path

    from zlscan.calib.file import load_calibration
    from zlscan.contracts.calibration import CaptureDescription
    from zlscan.recon.capture import reconstruct_capture
    from zlscan.recon.density import Grid

    p = argparse.ArgumentParser(prog="zlscan reconstruct")
    p.add_argument("--raw", required=True, type=Path)
    p.add_argument("--capture", required=True, type=Path)
    p.add_argument("--calibration", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--columns", required=True, type=int)
    p.add_argument("--pitch-x-m", required=True, type=float)
    p.add_argument("--pitch-y-m", required=True, type=float)
    p.add_argument("--channel", type=int, default=0)
    p.add_argument("--headerless-spp", type=int)
    p.add_argument("--quality", type=Path, help="observable quality event JSON")
    p.add_argument("--facets", type=Path, help="observable facet timing and calibration landmarks")
    p.add_argument(
        "--coding", choices=["offset_binary", "twos_complement"], default="offset_binary"
    )
    args = p.parse_args(list(argv))
    try:
        capture = CaptureDescription.model_validate_json(args.capture.read_bytes())
        cal = load_calibration(args.calibration)
        grid = Grid(args.columns, capture.lines, args.pitch_x_m, args.pitch_y_m)
        result = reconstruct_capture(
            args.raw,
            capture,
            cal,
            grid,
            args.out,
            channel=args.channel,
            headerless_spp=args.headerless_spp,
            coding=args.coding,
            quality_path=args.quality,
            facet_path=args.facets,
        )
    except (OSError, ValueError) as exc:
        print(f"重建失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    print(f"密度图已写入 {args.out}，有效覆盖 {result['valid_fraction']:.1%}")
    return 0 if result["valid_fraction"] > 0 else CHECK_FAIL_EXIT_CODE


def _run_metrics(argv: Sequence[str]) -> int:
    import json
    from pathlib import Path

    import numpy as np

    from zlscan.metrics.quality import compare_density

    p = argparse.ArgumentParser(prog="zlscan metrics")
    p.add_argument("--image", required=True, type=Path, help="density.npz from reconstruction")
    p.add_argument("--truth", required=True, type=Path, help="unblurred density reference (.npy)")
    p.add_argument("--optical", required=True, type=Path, help="optical density reference (.npy)")
    p.add_argument("--roi", type=Path, help="evaluation region mask (.npy); default is full frame")
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args(list(argv))
    try:
        with np.load(args.image) as image:
            density = image["density"]
            flags = image["flags"]
        region = np.load(args.roi) if args.roi else np.ones(density.shape, dtype=bool)
        result = compare_density(
            density,
            flags,
            {"unblurred": np.load(args.truth), "optical": np.load(args.optical)},
            region,
        )
        args.out.mkdir(parents=True)
        np.savez(args.out / "mask.npz", region=region, valid=result.evaluation_mask)
        manifest = {"mode": "metrics", "contains_truth": True, **result.metrics}
        (args.out / "run.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError) as exc:
        print(f"评价失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    print(f"评价已写入 {args.out}，有效覆盖 {manifest['valid_fraction']:.1%}")
    return 0 if manifest["valid_pixels"] else CHECK_FAIL_EXIT_CODE


def _run_cosim(argv: Sequence[str]) -> int:
    import json
    from pathlib import Path
    from subprocess import CalledProcessError, TimeoutExpired

    from zlscan.digital.cosim import run_capture_cosim

    p = argparse.ArgumentParser(prog="zlscan cosim")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--iverilog")
    p.add_argument("--vvp")
    args = p.parse_args(list(argv))
    try:
        result = run_capture_cosim(args.iverilog, args.vvp)
        args.out.mkdir(parents=True)
        (args.out / "run.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except (OSError, RuntimeError, CalledProcessError, TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        print(f"RTL 协同未完成：{detail}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    print(f"RTL 采集协同结果已写入 {args.out}")
    return 0 if result["passed"] else CHECK_FAIL_EXIT_CODE


def _run_report(argv: Sequence[str]) -> int:
    from pathlib import Path

    from zlscan.report.image_view import write_image_view

    p = argparse.ArgumentParser(prog="zlscan report")
    p.add_argument("--run", required=True, type=Path)
    p.add_argument("--raw", type=Path)
    p.add_argument("--compare", type=Path)
    p.add_argument("--compare-raw", type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args(list(argv))
    if args.compare_raw and not args.compare:
        p.error("--compare-raw requires --compare")
    try:
        write_image_view(
            args.run,
            args.out,
            raw_path=args.raw,
            comparison=args.compare,
            comparison_raw=args.compare_raw,
        )
    except (OSError, ValueError, KeyError) as exc:
        print(f"工作台生成失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    print(f"成像工作台已写入 {args.out}")
    return 0


def _run_replay(argv: Sequence[str]) -> int:
    from pathlib import Path

    from zlscan.config.presets import demo_config, nominal_config
    from zlscan.replay.saved import replay_saved_window

    p = argparse.ArgumentParser(prog="zlscan replay")
    p.add_argument(
        "--run", required=True, type=Path, help="directory from zlscan run --mode physical"
    )
    p.add_argument("--start-step", required=True, type=int)
    p.add_argument("--steps", required=True, type=int)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--preset", choices=["demo", "nominal"], default="demo")
    args = p.parse_args(list(argv))
    try:
        preset = nominal_config if args.preset == "nominal" else demo_config
        replay_saved_window(args.run, args.out, preset(), args.start_step, args.steps)
    except (OSError, ValueError) as exc:
        print(f"回放失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    print(f"回放结果已写入 {args.out}")
    return 0


def _run_serve(argv: Sequence[str]) -> int:
    from zlscan.engine.run_service import serve

    p = argparse.ArgumentParser(prog="zlscan serve")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--lines", type=int, default=6)
    args = p.parse_args(list(argv))
    serve(args.port, args.lines)
    return 0


HANDLERS = {"check": _run_check}
ARG_HANDLERS = {
    "budget": _run_budget,
    "run": _run_run,
    "calibrate": _run_calibrate,
    "reconstruct": _run_reconstruct,
    "metrics": _run_metrics,
    "report": _run_report,
    "cosim": _run_cosim,
    "replay": _run_replay,
    "serve": _run_serve,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zlscan",
        description="Laser film-scanner simulation. Sub-commands appear in the stage that "
        "implements them; unimplemented ones exit with a non-zero status.",
    )
    parser.add_argument("--version", action="version", version=f"zlscan {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for cmd in COMMANDS:
        sub.add_parser(
            cmd.name,
            help=cmd.summary,
            description=cmd.summary,
            add_help=cmd.name not in ARG_HANDLERS,
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args, rest = parser.parse_known_args(argv)
    if args.command in ARG_HANDLERS:
        return ARG_HANDLERS[args.command](rest)
    if args.command is None:
        parser.print_help(sys.stderr)
        return USAGE_EXIT_CODE
    handler = HANDLERS.get(args.command)
    if handler is not None:
        return handler()
    cmd = next(c for c in COMMANDS if c.name == args.command)
    print(
        f"zlscan {cmd.name}: 尚未实现（计划在阶段 {cmd.stage} 实现）",
        file=sys.stderr,
    )
    return NOT_IMPLEMENTED_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
