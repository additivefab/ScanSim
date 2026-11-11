"""Versioned connector pin tables of the Smart ZYNQ SP2 board.

The data is ``pin_registry.json``, imported byte for byte from the review package. It states what
the *board's* schematic shows; it says nothing about which ADC signal is wired where (every row
carries ``adc_assignment = "unknown; not an ADC wiring suggestion"``), so it can never approve a
connection. Physical pin number, logical index (``J5[n]``) and FPGA package pin are three
different things and are kept apart on purpose.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

DEFAULT_PATH = Path(__file__).with_name("pin_registry.json")
PINS_PER_CONNECTOR = 40
SIGNALS_PER_CONNECTOR = 34


class PinRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    board_revision: str
    connector: str
    physical_pin: int
    logical_index: int | None
    package_pin: str
    net_role: Literal["GPIO", "5V_DC", "VCC3V3", "GND"]
    bank: int
    vccio: str
    source: str
    adc_assignment: str


class PinRegistry:
    def __init__(self, rows: Iterable[PinRow]) -> None:
        self.rows: list[PinRow] = list(rows)
        self._by_key: dict[tuple[str, str, int], PinRow] = {}
        for row in self.rows:
            key = (row.board_revision, row.connector, row.physical_pin)
            if key in self._by_key:
                raise ValueError(f"duplicate pin {key}")
            self._by_key[key] = row
        tables: dict[tuple[str, str], list[PinRow]] = defaultdict(list)
        for row in self.rows:
            tables[(row.board_revision, row.connector)].append(row)
        for key, table in tables.items():
            if len(table) != PINS_PER_CONNECTOR:
                raise ValueError(
                    f"table {key} has {len(table)} pins, expected {PINS_PER_CONNECTOR} pins"
                )
            signals = sum(r.net_role == "GPIO" for r in table)
            if signals != SIGNALS_PER_CONNECTOR:
                raise ValueError(
                    f"table {key} has {signals} signal pins, expected {SIGNALS_PER_CONNECTOR}"
                )
        self._tables = dict(tables)

    @classmethod
    def load(cls, path: Path | None = None) -> PinRegistry:
        data = json.loads(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
        return cls(PinRow(**row) for row in data)

    def tables(self) -> list[tuple[str, str]]:
        return list(self._tables)

    def table(self, revision: str, connector: str) -> list[PinRow]:
        return sorted(self._tables[(revision, connector)], key=lambda r: r.physical_pin)

    def signal_pins(self, revision: str, connector: str) -> list[PinRow]:
        return [r for r in self.table(revision, connector) if r.net_role == "GPIO"]

    def find(self, revision: str, connector: str, physical_pin: int) -> PinRow | None:
        return self._by_key.get((revision, connector, physical_pin))

    def get(self, revision: str, connector: str, physical_pin: int) -> PinRow:
        row = self.find(revision, connector, physical_pin)
        if row is None:
            raise KeyError((revision, connector, physical_pin))
        return row

    def resolve_logical(self, revision: str, connector: str, logical_index: int) -> PinRow:
        """The row of ``<connector>[logical_index]``; its physical pin is generally different."""
        for row in self.signal_pins(revision, connector):
            if row.logical_index == logical_index:
                return row
        raise KeyError((revision, connector, logical_index))
