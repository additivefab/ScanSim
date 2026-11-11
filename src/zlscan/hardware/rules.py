"""Wiring and bank-voltage rules (docs/dev/stages/stage0.md S0-T06; plan section 6.2).

Everything here is a check on a *proposed* assignment against the board's pin tables. A check
that finds nothing wrong is not a pass: ``overall_verdict`` only returns PASS when the wiring has
been verified on the real hardware. Nothing here approves a connection.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from zlscan.contracts.verdict import Verdict
from zlscan.hardware.pins import PinRegistry

NEW_TABLE = "V1.3/V1.3B"
OLD_TABLE = "V1.0/V1.1"
_TABLE_OF_BOARD = {"V1.3": NEW_TABLE, "V1.3B": NEW_TABLE, "V1.0": OLD_TABLE, "V1.1": OLD_TABLE}

# J5 is BANK35 at a fixed 3.3 V. J6 is BANK33 on VADJ, nominally 3.3 V but to be measured.
J5_FIXED_V = 3.3
# Convention for recognising the same nominal I/O standard (3.3, 2.5, 1.8 V are >= 0.7 V apart);
# not a datasheet limit.
NOMINAL_MATCH_TOLERANCE_V = 0.1


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    verdict: Verdict


@dataclass(frozen=True)
class Assignment:
    """One signal proposed for one physical connector pin.

    ``table_revision`` records which revision's pin table the proposer used, and
    ``expected_package_pin`` the FPGA pin they believe it reaches; both are optional claims that
    are checked, never trusted.
    """

    signal: str
    connector: str
    physical_pin: int
    table_revision: str | None = None
    expected_package_pin: str | None = None


def table_revision_for(board_revision: str) -> str | None:
    """Pin table of a board revision; None when it has none (V1.2 is named but not tabulated)."""
    return _TABLE_OF_BOARD.get(board_revision)


def check_assignments(
    registry: PinRegistry, board_revision: str, assignments: Sequence[Assignment]
) -> list[Finding]:
    table = table_revision_for(board_revision)
    if table is None:
        return [
            Finding(
                "revision_not_covered",
                f"board revision {board_revision!r} has no pin table; nothing can be checked",
                Verdict.UNRESOLVED,
            )
        ]
    findings: list[Finding] = []
    taken: dict[tuple[str, int], str] = {}
    for a in assignments:
        where = f"{a.signal} on {a.connector} pin {a.physical_pin}"
        if a.table_revision is not None and a.table_revision != table:
            findings.append(
                Finding(
                    "wrong_revision_table",
                    f"{where}: taken from the {a.table_revision} table, but the board uses "
                    f"the {table} table",
                    Verdict.FAIL,
                )
            )
        row = registry.find(table, a.connector, a.physical_pin)
        if row is None:
            findings.append(
                Finding("no_such_pin", f"{where}: no such pin in the {table} table", Verdict.FAIL)
            )
            continue
        if row.net_role != "GPIO":
            findings.append(
                Finding(
                    "non_signal_pin",
                    f"{where}: that pin is {row.net_role}, not a signal",
                    Verdict.FAIL,
                )
            )
        if a.expected_package_pin is not None and a.expected_package_pin != row.package_pin:
            findings.append(
                Finding(
                    "package_pin_mismatch",
                    f"{where}: expected FPGA pin {a.expected_package_pin!r} but the table says "
                    f"{row.package_pin!r}",
                    Verdict.FAIL,
                )
            )
        key = (a.connector, a.physical_pin)
        if key in taken:
            findings.append(
                Finding(
                    "double_assigned",
                    f"{where}: already used by {taken[key]}",
                    Verdict.FAIL,
                )
            )
        else:
            taken[key] = a.signal
    return findings


def overall_verdict(findings: Sequence[Finding], wiring_verified: bool | None) -> Verdict:
    """FAIL beats everything; PASS needs wiring verified on the hardware, not merely no findings."""
    if any(f.verdict is Verdict.FAIL for f in findings):
        return Verdict.FAIL
    if any(f.verdict is Verdict.UNRESOLVED for f in findings):
        return Verdict.UNRESOLVED
    return Verdict.PASS if wiring_verified is True else Verdict.UNRESOLVED


def check_bank_voltage(
    connector: str, io_standard_v: float, measured_vadj_v: float | None = None
) -> Finding:
    """Compare an I/O standard's voltage with what the connector's bank really supplies."""
    if connector == "J5":
        ok = math.isclose(io_standard_v, J5_FIXED_V, abs_tol=NOMINAL_MATCH_TOLERANCE_V)
        return Finding(
            "bank35_voltage",
            f"J5 (BANK35) is fixed at {J5_FIXED_V} V; the I/O standard needs {io_standard_v} V",
            Verdict.PASS if ok else Verdict.FAIL,
        )
    if connector == "J6":
        if measured_vadj_v is None:
            return Finding(
                "vadj_unknown",
                "J6 (BANK33) runs on VADJ, which has not been measured; the nominal 3.3 V is "
                "not evidence",
                Verdict.UNRESOLVED,
            )
        ok = math.isclose(io_standard_v, measured_vadj_v, abs_tol=NOMINAL_MATCH_TOLERANCE_V)
        return Finding(
            "bank33_voltage",
            f"J6 (BANK33) measured VADJ {measured_vadj_v} V; "
            f"the I/O standard needs {io_standard_v} V",
            Verdict.PASS if ok else Verdict.FAIL,
        )
    return Finding(
        "unknown_connector", f"no bank information for {connector!r}", Verdict.UNRESOLVED
    )
