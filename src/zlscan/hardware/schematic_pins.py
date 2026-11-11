"""Independent reading of the SP2 schematic's connector tables, to check the pin registry.

Page 10 of the schematic draws each 40-pin header as a symbol: pin numbers sit in two columns,
net names beside them. ``pdftotext -bbox`` gives every word with its coordinates; this module
pairs names with pin numbers by position, which is independent of how the review package built
its table. Page 12 holds a constraints reference mapping ``J5[n]``/``J6[n]`` to FPGA pins.

Geometry constants were measured on page 10 of SmartZynq_SP2_Schematic_V1d3B_20260413.pdf (the
page's coordinates run to x of about 740, wider than its nominal 612 pt). A GND pin is drawn as a
symbol with no net-name text, and a few power names are drawn away from their pin number; those
cases are reported as such, never guessed.

Pure functions only; the ``pdftotext`` call lives in tools/review_pin_registry.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from zlscan.hardware.pins import PinRegistry, PinRow

_WORD = re.compile(
    r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="[\d.]+" yMax="[\d.]+">([^<]*)</word>'
)
_NAME = re.compile(r"^(GPIO_[A-Z]{1,2}\d{1,2}_L\d+[PN]|5V_DC|VCC3V3)$")
_GPIO = re.compile(r"^GPIO_([A-Z]{1,2}\d{1,2})_L\d+[PN]$")
# A "#J6/<n> =" hint counts only when it is on the same line: page 12 prints some comments as a
# block below the port lines, and [ \t] (not \s) keeps a comment from crossing a newline.
_XDC = re.compile(
    r"PACKAGE_PIN\s+(\w+)\s+\[get_ports\s+\{(J[56])\[(\d+)\]\}\];"
    r"(?:[ \t]*#[ \t]*(?:J6/(\d+)[ \t]*=)?)?"
)

# A name is level with its pin number to within this many points (names sit ~1 pt above).
# Power symbols on the old J5 are drawn 2.4 pt or more away and stacked, so they do not pair.
Y_TOLERANCE = 1.5


@dataclass(frozen=True)
class Word:
    x: float
    y: float
    text: str


@dataclass(frozen=True)
class Region:
    revision: str
    connector: str
    number_x: tuple[float, float]
    name_x: tuple[float, float]
    y: tuple[float, float]


REGIONS: dict[str, Region] = {
    "V1.3/V1.3B J5": Region("V1.3/V1.3B", "J5", (140, 260), (60, 330), (60, 250)),
    "V1.0/V1.1 J5": Region("V1.0/V1.1", "J5", (498, 620), (420, 700), (60, 250)),
    "V1.3/V1.3B J6": Region("V1.3/V1.3B", "J6", (140, 260), (60, 330), (330, 510)),
}


def parse_bbox_words(html: str) -> list[Word]:
    return [Word(float(x), float(y), text) for x, y, text in _WORD.findall(html)]


def split_net_name(text: str) -> tuple[str, str]:
    """('GPIO', 'G16') for GPIO_G16_L4N; ('5V_DC', '') for a power net."""
    gpio = _GPIO.match(text)
    if gpio:
        return "GPIO", gpio.group(1)
    if text in ("5V_DC", "VCC3V3"):
        return text, ""
    raise ValueError(f"not a connector net name: {text!r}")


@dataclass
class TableReading:
    names: dict[int, str]  # pin -> net-name text aligned with that pin's number
    unpaired: list[str] = field(default_factory=list)  # names in the region no pin number claimed
    ambiguous: list[int] = field(default_factory=list)  # pins claimed by two different names


def read_table(words: list[Word], region: Region) -> TableReading:
    in_y = lambda w: region.y[0] <= w.y <= region.y[1]  # noqa: E731
    numbers = [
        w
        for w in words
        if w.text.isdigit()
        and 1 <= int(w.text) <= 40
        and region.number_x[0] <= w.x <= region.number_x[1]
        and in_y(w)
    ]
    names = [
        w
        for w in words
        if _NAME.match(w.text) and region.name_x[0] <= w.x <= region.name_x[1] and in_y(w)
    ]
    centre = sum(w.x for w in numbers) / len(numbers) if numbers else 0.0
    claimed: dict[int, set[str]] = {}
    unpaired: list[str] = []
    for item in names:
        wants_even = item.x < centre  # left of the symbol: even pins; right: odd pins
        candidates = [
            n
            for n in numbers
            if abs(n.y - item.y) <= Y_TOLERANCE and (int(n.text) % 2 == 0) == wants_even
        ]
        if not candidates:
            unpaired.append(item.text)
            continue
        best = min(candidates, key=lambda n: (abs(n.y - item.y), abs(n.x - item.x)))
        claimed.setdefault(int(best.text), set()).add(item.text)
    reading = TableReading(names={}, unpaired=sorted(unpaired))
    for pin, texts in sorted(claimed.items()):
        if len(texts) == 1:
            reading.names[pin] = next(iter(texts))
        else:
            reading.ambiguous.append(pin)
    return reading


class RowStatus(StrEnum):
    CONFIRMED = "confirmed"
    MISMATCH = "mismatch"
    GND_SYMBOL_NO_TEXT = "gnd_symbol_no_text"
    POWER_NAME_NOT_ALIGNED = "power_name_not_aligned"
    UNEXPLAINED = "unexplained"


@dataclass(frozen=True)
class RowResult:
    revision: str
    connector: str
    physical_pin: int
    registry_role: str
    registry_package_pin: str
    page_text: str | None
    status: RowStatus


def compare_table(rows: list[PinRow], reading: TableReading) -> list[RowResult]:
    results = []
    for row in sorted(rows, key=lambda r: r.physical_pin):
        text = reading.names.get(row.physical_pin)
        if text is not None:
            role, package_pin = split_net_name(text)
            same = role == row.net_role and (role != "GPIO" or package_pin == row.package_pin)
            status = RowStatus.CONFIRMED if same else RowStatus.MISMATCH
        elif row.physical_pin in reading.ambiguous:
            status = RowStatus.UNEXPLAINED
        elif row.net_role == "GND":
            status = RowStatus.GND_SYMBOL_NO_TEXT
        elif row.net_role in reading.unpaired:
            status = RowStatus.POWER_NAME_NOT_ALIGNED
        else:
            status = RowStatus.UNEXPLAINED
        results.append(
            RowResult(
                row.board_revision,
                row.connector,
                row.physical_pin,
                row.net_role,
                row.package_pin,
                text,
                status,
            )  # fmt: skip
        )
    return results


# ---- the constraints reference on page 12 ---------------------------------------------------


@dataclass(frozen=True)
class XdcEntry:
    connector: str
    logical_index: int
    package_pin: str
    physical_hint: int | None  # from a trailing "#J6/<n> =" comment, when present


def parse_xdc(text: str) -> list[XdcEntry]:
    return [
        XdcEntry(conn, int(index), pin, int(hint) if hint else None)
        for pin, conn, index, hint in _XDC.findall(text)
    ]


class XdcStatus(StrEnum):
    CONFIRMED = "confirmed"
    MISMATCH = "mismatch"
    NOT_IN_XDC = "not_in_xdc"


@dataclass(frozen=True)
class XdcResult:
    revision: str
    connector: str
    logical_index: int
    physical_pin: int
    status: XdcStatus
    detail: str


def compare_xdc(registry: PinRegistry, entries: list[XdcEntry]) -> list[XdcResult]:
    """Check every registry signal row's logical index and package pin against the XDC reference."""
    by_index: dict[tuple[str, int], list[XdcEntry]] = {}
    for entry in entries:
        by_index.setdefault((entry.connector, entry.logical_index), []).append(entry)
    results = []
    for revision, connector in registry.tables():
        for row in registry.signal_pins(revision, connector):
            found = by_index.get((connector, row.logical_index), [])
            if not found:
                status, detail = XdcStatus.NOT_IN_XDC, "no PACKAGE_PIN line for this index"
            else:
                problems = []
                xdc_pins = sorted({e.package_pin for e in found})
                if any(pin != row.package_pin for pin in xdc_pins):
                    problems.append(f"XDC pin {xdc_pins} vs registry {row.package_pin}")
                hints = {e.physical_hint for e in found if e.physical_hint is not None}
                if connector == "J6" and hints and hints != {row.physical_pin}:
                    problems.append(f"XDC says J6 pin {sorted(hints)}, registry {row.physical_pin}")
                status = XdcStatus.MISMATCH if problems else XdcStatus.CONFIRMED
                detail = "; ".join(problems) if problems else "package pin matches"
            results.append(
                XdcResult(revision, connector, row.logical_index, row.physical_pin, status, detail)
            )
    return results
