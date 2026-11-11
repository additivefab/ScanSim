"""Re-derive the pin tables from the real schematic and check the registry against it.

Needs ``pdftotext`` (poppler); skipped with a visible reason if it is not installed. The 18
power and GND rows cannot be settled by text position; they were checked by eye and are
documented in docs/reports/pin_registry_review.md, so this test pins their count instead.
"""

import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from zlscan.hardware import schematic_pins as sp
from zlscan.hardware.pins import PinRegistry

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "legacy/originals/SmartZynq_SP2_Schematic_V1d3B_20260413.pdf.pdf"

pytestmark = pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext not installed")


def _text(args, page, tmp_path):
    out = tmp_path / f"p{page}.txt"
    subprocess.run(
        ["pdftotext", *args, "-f", str(page), "-l", str(page), str(PDF), str(out)],
        check=True,
        capture_output=True,
    )
    return out.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("schematic")
    registry = PinRegistry.load()
    words = sp.parse_bbox_words(_text(["-bbox"], 10, tmp))
    rows = []
    for region in sp.REGIONS.values():
        reading = sp.read_table(words, region)
        rows += sp.compare_table(registry.table(region.revision, region.connector), reading)
    xdc = sp.compare_xdc(registry, sp.parse_xdc(_text(["-layout"], 12, tmp)))
    return rows, xdc


def test_every_gpio_row_matches_the_schematic_table(results):
    rows, _ = results
    gpio = [r for r in rows if r.registry_role == "GPIO"]
    assert len(gpio) == 102
    assert Counter(r.status for r in gpio) == {sp.RowStatus.CONFIRMED: 102}


def test_nothing_in_the_registry_contradicts_the_page(results):
    rows, _ = results
    bad = [r for r in rows if r.status in (sp.RowStatus.MISMATCH, sp.RowStatus.UNEXPLAINED)]
    assert bad == []


def test_exactly_the_power_and_ground_rows_are_left_for_the_eye(results):
    rows, _ = results
    left = Counter(
        (r.registry_role, r.status) for r in rows if r.status is not sp.RowStatus.CONFIRMED
    )
    assert left == {
        ("5V_DC", sp.RowStatus.POWER_NAME_NOT_ALIGNED): 6,
        ("VCC3V3", sp.RowStatus.POWER_NAME_NOT_ALIGNED): 6,
        ("GND", sp.RowStatus.GND_SYMBOL_NO_TEXT): 6,
    }


def test_every_logical_index_matches_the_constraints_reference(results):
    _, xdc = results
    assert len(xdc) == 102
    assert Counter(r.status for r in xdc) == {sp.XdcStatus.CONFIRMED: 102}
