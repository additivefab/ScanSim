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
    loaded = None
    try:
        config = json.loads(args.config.read_bytes()) if args.config else {}
        if not isinstance(config, dict):
            raise ValueError("budget configuration must be an object")
        if config.get("format") == "zlscan.research-scenario/1":
            from zlscan.engine.research_config import load_scenario, scenario_budget

            loaded = load_scenario(args.config)
            config = scenario_budget(loaded)
        elif "format" in config:
            raise ValueError("unsupported budget configuration format")
    except (ValueError, OSError, TypeError) as exc:
        print(f"配置拒绝：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    if args.mode:
        config["mode"] = args.mode
    try:
        result = run_budget(config)
    except BudgetRefused as exc:
        print("hardware_constrained 拒绝运行：", file=sys.stderr)
        for line in exc.problems:
            print(f"  - {line}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    if loaded is not None:
        result["scenario_configuration"] = loaded.provenance()
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
    p.add_argument(
        "--mode", choices=["digital", "physical", "calibration", "recording"], required=True
    )
    p.add_argument("--out", type=Path)
    p.add_argument("--lines", type=int)
    p.add_argument("--preset", choices=["demo", "nominal"])
    p.add_argument("--config", type=Path, help="versioned, assumed research scenario JSON")
    p.add_argument("--stream", action="store_true", help="stream two physical ADC channels")
    p.add_argument("--resume", type=Path, help="resume a committed physical recording")
    p.add_argument("--commands", help="recording JSONL path or - for stdin; EOF pauses")
    args = p.parse_args(list(argv))
    if args.mode == "recording":
        if args.lines is not None or args.preset is not None or args.stream:
            p.error("recording uses its saved definition, not --lines/--preset/--stream")
        if args.resume and args.config:
            p.error("recording --resume uses preserved configuration; omit --config")
        from contextlib import nullcontext

        from zlscan.engine.recording_cli import run_recording

        try:
            source = (
                open(args.commands, encoding="utf-8")
                if args.commands and args.commands != "-"
                else nullcontext(sys.stdin)
            )
            with source as commands:
                return run_recording(
                    args.config, args.resume, args.out, commands, sys.stdout, sys.stderr
                )
        except OSError as exc:
            print(f"无法读取录制命令：{exc}", file=sys.stderr)
            return CHECK_FAIL_EXIT_CODE
    if args.resume is not None or args.commands is not None:
        p.error("--resume/--commands require recording mode")
    args.out = args.out or Path("run_out")
    if args.stream and args.mode != "physical":
        p.error("--stream requires physical mode")
    loaded = None
    if args.config:
        if args.lines is not None or args.preset is not None or args.stream:
            p.error("--config cannot be combined with --lines, --preset or --stream")
        if args.mode == "digital":
            p.error("research --config requires physical or calibration mode")
        from zlscan.engine.research_config import load_scenario

        try:
            loaded = load_scenario(args.config)
        except (ValueError, OSError, TypeError) as exc:
            print(f"配置拒绝：{exc}", file=sys.stderr)
            return CHECK_FAIL_EXIT_CODE
        args.lines = loaded.document.lines
        args.stream = loaded.secondary is not None
        if args.mode == "calibration" and args.stream:
            p.error("calibration scenario requires one channel")
        if args.out.exists():
            print("配置运行拒绝覆盖已有输出目录", file=sys.stderr)
            return CHECK_FAIL_EXIT_CODE
    else:
        args.lines = args.lines if args.lines is not None else 8
    preset = nominal_config if args.preset == "nominal" else demo_config
    primary = loaded.primary if loaded else preset()
    if args.mode == "calibration":
        from zlscan.experiment.calibration_scan import run_calibration_scan

        if args.lines < 2:
            p.error("calibration mode requires at least two lines per target")
        run = run_calibration_scan(args.out, primary, lines_per_target=args.lines)
    elif args.mode == "physical":
        from zlscan.engine.physical_run import run_physical

        if args.lines < 1:
            p.error("--lines must be positive")
        if args.stream:
            from zlscan.engine.stream_physical import stream_physical

            secondary = loaded.secondary if loaded else replace(primary, seed=primary.seed + 1)
            run = stream_physical(args.out, primary, secondary, args.lines)
        else:
            run = run_physical(args.out, primary, args.lines, save_checkpoints=True)
    else:
        run = run_digital(args.out)
    if loaded is not None:
        from zlscan.engine.research_config import persist_scenario

        run = persist_scenario(loaded, args.out, run, mode=args.mode)
    print(
        f"{run['mode']} 模式完成：重建 {run['recon_status']}，"
        f"完整性 {run['integrity']}，写入 {args.out}"
    )
    if "physical_applicability" in run:
        print("物理适用性：受限研究模型；不作为高密度测量能力或实机精度验证。")
        applicability = run["physical_applicability"]
        rows = applicability if isinstance(applicability, list) else [applicability]
        if any(row["stochastic_log_status"] == "grid_dependent_exploration_only" for row in rows):
            print("注意：带噪对数输入缺少固定物理响应，统计量依赖数值时间格。")
    return 0 if run["recon_status"] == "complete" else CHECK_FAIL_EXIT_CODE


def _run_calibrate(argv: Sequence[str]) -> int:
    import json
    from pathlib import Path

    from zlscan.calib.file import save_calibration
    from zlscan.calib.observations import calibration_from_scan

    p = argparse.ArgumentParser(prog="zlscan calibrate")
    p.add_argument("--raw", required=True, type=Path)
    p.add_argument("--protocol", required=True, type=Path)
    p.add_argument(
        "--transfer-method",
        choices=["log", "monotonic"],
        default="log",
        help="radiometric model; monotonic requires declared reference uncertainty",
    )
    p.add_argument("--timing", type=Path, help="synchronized injection observation JSON")
    p.add_argument("--edge-registration", action="store_true", help="fit optical edge registration")
    p.add_argument("--quality", type=Path, help="observable quality event JSON")
    p.add_argument("--facets", type=Path, help="observable facet timing and calibration landmarks")
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args(list(argv))
    try:
        cal = calibration_from_scan(
            args.raw,
            args.protocol,
            fit_transfer=True,
            transfer_method=args.transfer_method,
            timing_path=args.timing,
            quality_path=args.quality,
            facet_path=args.facets,
            edge_registration=args.edge_registration,
        )
        args.out.mkdir(parents=True)
        save_calibration(args.out / "calibration.json", cal)
        summary = {
            "mode": "calibrate",
            "calibration_id": cal.calibration_id,
            "provenance": cal.provenance.value,
            "items": list(cal.items),
            "transfer_reference_assessments": {
                channel: transfer["reference_assessment"]
                for channel, transfer in cal.items["transfer_lut"].value["channels"].items()
            },
        }
        (args.out / "run.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"标定失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    if any(a["status"] == "mismatch" for a in summary["transfer_reference_assessments"].values()):
        print("参考传递拟合未满足协议容差；已保存诊断，重建将拒绝该通道", file=sys.stderr)
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

    from zlscan.io.manifest import sha256_file
    from zlscan.metrics.quality import compare_density
    from zlscan.metrics.targets import evaluate_targets

    p = argparse.ArgumentParser(prog="zlscan metrics")
    p.add_argument("--image", required=True, type=Path, help="density.npz from reconstruction")
    p.add_argument("--truth", required=True, type=Path, help="unblurred density reference (.npy)")
    p.add_argument("--optical", required=True, type=Path, help="optical density reference (.npy)")
    p.add_argument("--roi", type=Path, help="evaluation region mask (.npy); default is full frame")
    p.add_argument(
        "--targets", type=Path, help="quality target manifest (wedge, MTF, noise, geometry)"
    )
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
        manifest = {
            "mode": "metrics",
            "algorithm": "density-common-mask-v1",
            "contains_truth": True,
            **result.metrics,
        }
        masks = {"region": region, "valid": result.evaluation_mask}
        manifest["inputs"] = {
            str(path.resolve()): sha256_file(path)
            for path in (args.image, args.truth, args.optical, args.roi)
            if path is not None
        }
        if args.targets is not None:
            targets, target_masks, hashes = evaluate_targets(
                args.targets, density, result.evaluation_mask
            )
            manifest.update(
                target_metrics=targets, target_inputs=hashes, target_algorithm="quality-targets-v1"
            )
            masks.update(target_masks)
        serialized = json.dumps(manifest, indent=2, allow_nan=False) + "\n"
        args.out.mkdir(parents=True)
        np.savez(args.out / "mask.npz", **masks)
        (args.out / "run.json").write_text(serialized, encoding="utf-8")
    except (OSError, ValueError, KeyError) as exc:
        print(f"评价失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    print(f"评价已写入 {args.out}，有效覆盖 {manifest['valid_fraction']:.1%}")
    complete = all(
        value["status"] == "computed" for value in manifest.get("target_metrics", {}).values()
    )
    return 0 if manifest["valid_pixels"] and complete else CHECK_FAIL_EXIT_CODE


def _run_sweep(argv: Sequence[str]) -> int:
    import json
    from pathlib import Path

    from zlscan.experiment.sweep import run_sweep

    p = argparse.ArgumentParser(prog="zlscan sweep")
    p.add_argument("--config", required=True, type=Path, help="JSON with preset, lines and axes")
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args(list(argv))
    try:
        spec = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise ValueError("sweep specification must be a JSON object")
        result = run_sweep(
            args.out,
            preset=spec.get("preset", "demo"),
            parameter_values=spec["axes"],
            lines=spec.get("lines", 2),
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"扫描失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    print(f"扫描完成：{len(result['points'])} 个点，写入 {args.out}")
    return 0


def _run_compare(argv: Sequence[str]) -> int:
    import json
    from pathlib import Path

    from zlscan.metrics.compare_runs import compare_runs

    p = argparse.ArgumentParser(prog="zlscan compare")
    p.add_argument("--first", required=True, type=Path)
    p.add_argument("--second", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args(list(argv))
    try:
        if any(
            args.out.resolve().is_relative_to(run.resolve()) for run in (args.first, args.second)
        ):
            raise ValueError("comparison output must be outside both input runs")
        result = compare_runs(args.first, args.second)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"比较失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    print(f"比较结果已写入 {args.out}")
    return 0


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
    import json
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
    p.add_argument("--preset", choices=["demo", "nominal"], default=None)
    p.add_argument("--probes", default="", help="recording replay probe names, comma separated")
    args = p.parse_args(list(argv))
    try:
        mode = (
            "physical_recording"
            if (args.run / "definition.json").is_file() and (args.run / "checkpoint.json").is_file()
            else json.loads((args.run / "run.json").read_text()).get("mode")
        )
        if mode == "physical_recording":
            if args.preset:
                raise ValueError("recording replay uses its saved configuration; omit --preset")
            from zlscan.engine.recording_replay import replay_recording

            replay_recording(
                args.run,
                args.out,
                args.start_step,
                args.steps,
                set(filter(None, args.probes.split(","))),
            )
            print(f"回放结果已写入 {args.out}")
            return 0
        if args.probes:
            raise ValueError("--probes currently requires a recording source")
        preset = nominal_config if args.preset == "nominal" else demo_config
        replay_saved_window(
            args.run, args.out, preset() if args.preset else None, args.start_step, args.steps
        )
    except (OSError, ValueError) as exc:
        print(f"回放失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    print(f"回放结果已写入 {args.out}")
    return 0


def _run_serve(argv: Sequence[str]) -> int:
    from pathlib import Path

    from zlscan.engine.run_service import serve

    p = argparse.ArgumentParser(prog="zlscan serve")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--lines", type=int, default=6)
    selection = p.add_mutually_exclusive_group()
    selection.add_argument("--images", type=Path, help="read-only reconstruction registry JSON")
    selection.add_argument(
        "--run", type=Path, help="read-only saved physical run or frozen recording"
    )
    args = p.parse_args(list(argv))
    try:
        serve(args.port, args.lines, args.images, args.run)
    except (OSError, KeyError, ValueError) as exc:
        print(f"工作台启动失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    return 0


def _run_ident(argv: Sequence[str]) -> int:
    import hashlib
    import json
    from pathlib import Path

    from zlscan.ident.observe import attach_scope_observer
    from zlscan.ident.raw import RawMetadata, import_raw_capture, write_raw_import_report
    from zlscan.ident.scope import ScopeMetadata, import_scope_csv
    from zlscan.ident.static import StaticFitConfig, identify_static
    from zlscan.ident.table import TableMetadata, import_measurement_table
    from zlscan.observation.scope import ScopeObserverConfig
    from zlscan.report.static_identification import write_static_residuals

    p = argparse.ArgumentParser(prog="zlscan ident", description="观测导入与显式分区静态/动态辨识")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--dynamic-fit",
        type=Path,
        help="explicit multi-capture observed step-response identification JSON",
    )
    source.add_argument(
        "--timing-fit", type=Path, help="observable counter-tagged timing identification JSON"
    )
    source.add_argument("--csv", type=Path)
    source.add_argument("--table", type=Path, help="标量测量模板 CSV")
    source.add_argument("--raw", type=Path, help="ZLRAW 0.1 草案；未知 FPGA 格式不支持")
    p.add_argument(
        "--static-fit", type=Path, help="explicit paired scalar linear identification JSON"
    )
    p.add_argument("--model-waveform", type=Path, help="model CSV: time_s,voltage_v")
    p.add_argument("--observer", type=Path, help="explicit candidate scope response JSON")
    p.add_argument("--observe-channel", help="one voltage channel for nominal residuals")
    p.add_argument("--metadata", type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args(list(argv))
    if args.dynamic_fit is None and args.timing_fit is None and args.metadata is None:
        p.error("--metadata is required for --csv/--table/--raw")
    if args.timing_fit is not None:
        if any(
            (
                args.dynamic_fit,
                args.metadata,
                args.static_fit,
                args.model_waveform,
                args.observer,
                args.observe_channel,
            )
        ):
            p.error("--timing-fit uses only its explicit capture declarations")
        from zlscan.ident.timing import TimingFitConfig, identify_timing_files
        from zlscan.report.timing_identification import write_timing_residuals

        try:
            blob = args.timing_fit.read_bytes()
            config = TimingFitConfig.model_validate_json(blob)
            result = identify_timing_files(config, args.timing_fit.parent)
            result["config_sha256"] = hashlib.sha256(blob).hexdigest()
            args.out.mkdir(parents=True, exist_ok=False)
            (args.out / "run.json").write_text(
                json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n"
            )
            write_timing_residuals(result, args.out / "timing_residuals.svg")
        except (OSError, ValueError) as exc:
            print(f"时基辨识失败：{exc}", file=sys.stderr)
            return CHECK_FAIL_EXIT_CODE
        print(f"观测时基辨识已写入 {args.out}；不自动升级实测证据")
        return (
            0
            if result["status"] == "identified"
            and result["validation"]["status"] not in {"mismatch", "unidentifiable"}
            else CHECK_FAIL_EXIT_CODE
        )
    if args.dynamic_fit is not None:
        if any(
            (
                args.metadata,
                args.static_fit,
                args.model_waveform,
                args.observer,
                args.observe_channel,
            )
        ):
            p.error("--dynamic-fit uses only its explicit per-capture declarations")
        from zlscan.ident.dynamic import DynamicFitConfig, identify_dynamic_files
        from zlscan.report.dynamic_identification import write_dynamic_residuals

        try:
            blob = args.dynamic_fit.read_bytes()
            config = DynamicFitConfig.model_validate_json(blob)
            result = identify_dynamic_files(config, args.dynamic_fit.parent)
            result["config_sha256"] = hashlib.sha256(blob).hexdigest()
            args.out.mkdir(parents=True, exist_ok=False)
            (args.out / "run.json").write_text(
                json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n"
            )
            write_dynamic_residuals(result, args.out / "dynamic_residuals.svg")
        except (OSError, ValueError) as exc:
            print(f"动态辨识失败：{exc}", file=sys.stderr)
            return CHECK_FAIL_EXIT_CODE
        print(f"动态观测辨识已写入 {args.out}；不自动升级实测证据")
        return (
            0
            if result["status"] == "identified"
            and result["validation"]["status"] not in {"mismatch", "unidentifiable"}
            else CHECK_FAIL_EXIT_CODE
        )
    observation_args = [args.model_waveform, args.observer, args.observe_channel]
    if any(observation_args) and (not all(observation_args) or args.csv is None):
        p.error("--model-waveform/--observer/--observe-channel require each other and --csv")
    if args.static_fit is not None and args.table is None:
        p.error("--static-fit requires --table")
    try:
        declaration = args.metadata.read_bytes()
        if args.raw is not None:
            raw_metadata = RawMetadata.model_validate_json(declaration)
            result = import_raw_capture(args.raw, raw_metadata, args.out)
            write_raw_import_report(result, declaration, args.out)
            if result["integrity"] == "unreadable":
                print(f"码流不可读；副本与诊断已保存于 {args.out}", file=sys.stderr)
                return CHECK_FAIL_EXIT_CODE
        else:
            if args.table is not None:
                table_metadata = TableMetadata.model_validate_json(declaration)
                result = import_measurement_table(args.table, table_metadata, args.metadata.parent)
                if args.static_fit is not None:
                    fit_bytes = args.static_fit.read_bytes()
                    fit_config = StaticFitConfig.model_validate_json(fit_bytes)
                    result["static_identification"] = identify_static(result, fit_config)
                    result["static_fit_config_sha256"] = hashlib.sha256(fit_bytes).hexdigest()
            else:
                metadata = ScopeMetadata.model_validate_json(declaration)
                result = import_scope_csv(args.csv, metadata)
                if args.observer is not None:
                    observer_bytes = args.observer.read_bytes()
                    observer = ScopeObserverConfig.model_validate_json(observer_bytes)
                    model = import_scope_csv(
                        args.model_waveform,
                        ScopeMetadata(
                            time_column="time_s",
                            time_unit="s",
                            channels=[{"column": "voltage_v", "unit": "V"}],
                        ),
                    )
                    attachment = attach_scope_observer(
                        result,
                        model["time_s"],
                        model["channels"][0]["values_si"],
                        observer,
                        args.observe_channel,
                    )
                    attachment["model_waveform_sha256"] = model["source_sha256"]
                    attachment["observer_config_sha256"] = hashlib.sha256(
                        observer_bytes
                    ).hexdigest()
                    result["observation_model"] = attachment
            result["metadata_sha256"] = hashlib.sha256(declaration).hexdigest()
            args.out.mkdir(parents=True, exist_ok=False)
            with (args.out / "run.json").open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, allow_nan=False)
            if "static_identification" in result:
                identified = result["static_identification"]
                write_static_residuals(identified, args.out / "static_residuals.svg")
                if identified["status"] != "identified" or identified.get("validation", {}).get(
                    "status"
                ) in {"mismatch", "unidentifiable"}:
                    print(f"辨识未通过；诊断已保存于 {args.out}", file=sys.stderr)
                    return CHECK_FAIL_EXIT_CODE
    except (OSError, ValueError) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return CHECK_FAIL_EXIT_CODE
    print(f"导入已写入 {args.out}；同步={result['synchronization']}；不自动升级实测证据")
    return 0


HANDLERS = {"check": _run_check}
ARG_HANDLERS = {
    "ident": _run_ident,
    "sweep": _run_sweep,
    "compare": _run_compare,
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
