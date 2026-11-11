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
    return (
        0
        if gate.status is ProfileStatus.READY_FOR_FURTHER_VALIDATION
        else CHECK_UNRESOLVED_EXIT_CODE
    )


HANDLERS = {"check": _run_check}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zlscan",
        description="Laser film-scanner simulation. Sub-commands appear in the stage that "
        "implements them; unimplemented ones exit with a non-zero status.",
    )
    parser.add_argument("--version", action="version", version=f"zlscan {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for cmd in COMMANDS:
        sub.add_parser(cmd.name, help=cmd.summary, description=cmd.summary)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args, _ = parser.parse_known_args(argv)
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
