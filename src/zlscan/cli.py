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
    cmd = next(c for c in COMMANDS if c.name == args.command)
    print(
        f"zlscan {cmd.name}: 尚未实现（计划在阶段 {cmd.stage} 实现）",
        file=sys.stderr,
    )
    return NOT_IMPLEMENTED_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
