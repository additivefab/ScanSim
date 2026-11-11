"""The first-level workbench: one offline page, honest about what it does not know."""

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from zlscan.contracts.events import Derivation, Event, Origin, Visibility
from zlscan.contracts.param import Evidence, Param
from zlscan.evidence.table import load_parameters
from zlscan.report.viewer import COMPONENTS, build_viewer, build_viewer_from_run_dir

EVENT = Event(
    kind="SOS", origin=Origin.SIMULATED, visibility=Visibility.OBSERVABLE,
    derivation=Derivation.SENSOR, capability_ref="hardware.control.sos_sensor",
    clock_domain=0, count=3, tick=123_456_789_012, session_id=1, epoch=0,
)  # fmt: skip
ORACLE = Event(
    kind="FACET_TRUE", origin=Origin.SIMULATED, visibility=Visibility.ORACLE_ONLY,
    derivation=Derivation.INFERRED, clock_domain=0, count=3, tick=5, session_id=1, epoch=0,
)  # fmt: skip
META = {"mode": "research", "preset": "demo", "run_id": "r1", "session_id": 1, "epoch": 0}


@pytest.fixture(scope="module")
def page():
    return build_viewer(load_parameters(), [EVENT, ORACLE], META)


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text, self.tags = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def parse(html):
    p = Text()
    p.feed(html)
    return p


# ---- W12: offline ---------------------------------------------------------------------------


def test_the_page_needs_nothing_from_outside(page):
    banned = [
        r'src\s*=\s*["\']?(https?:)?//', r'href\s*=\s*["\']?(https?:)?//',
        r"url\(\s*[\"']?https?:", r"@import", r"\bfetch\s*\(", r"XMLHttpRequest",
        r"\bimport\s*\(", r"<link\b", r"<iframe", r"navigator\.sendBeacon", r"WebSocket",
    ]  # fmt: skip
    for pattern in banned:
        assert not re.search(pattern, page, re.IGNORECASE), pattern
    tags = {t for t, _ in parse(page).tags}
    assert "script" in tags and "link" not in tags


def test_every_script_is_inline(page):
    scripts = [a for t, a in parse(page).tags if t == "script"]
    assert scripts and all("src" not in a for a in scripts)


def test_the_page_is_utf8_and_declares_a_viewport(page):
    assert '<meta charset="utf-8">' in page and 'name="viewport"' in page


# ---- unknown stays unknown ------------------------------------------------------------------


def test_an_unknown_value_is_shown_as_unknown_never_as_zero(page):
    text = " ".join(parse(page).text)
    assert "未知" in text
    # laser wavelength is unknown in the table: its row must say so
    row = re.search(r"激光波长.{0,400}", text, re.DOTALL)
    assert row and "未知" in row.group(0)
    assert "<td>0</td>" not in page and "0 nm" not in page


def test_a_parameter_forced_to_none_shows_unknown_in_its_probe_row():
    params = load_parameters()
    params["board.revision"] = Param(unit="")
    html = build_viewer(params, [], META)
    assert re.search(r"开发板版本</td><td[^>]*>未知", html)
    assert "V1.3B" not in html


def test_a_known_value_shows_its_evidence_in_chinese():
    html = build_viewer(load_parameters(), [], META)
    assert "V1.3B" in html and "图片观察" in html
    assert "原理图标称" in html
    params = load_parameters()
    params["laser.power_w"] = Param(value=0.001, unit="W", evidence=Evidence.ASSUMED)
    assert "假设" in build_viewer(params, [], META)


def test_every_component_appears_in_the_tree_and_has_a_probe_panel(page):
    for comp in COMPONENTS:
        assert f'data-component="{comp.id}"' in page
        assert f'id="probe-{comp.id}"' in page


# ---- evidence and honesty labels ------------------------------------------------------------


def test_the_five_kinds_of_pass_are_shown_separately_and_not_executed_by_default(page):
    text = " ".join(parse(page).text)
    for label in ("文档一致性", "行为模型", "RTL 功能协同", "综合与时序", "实机验证"):
        assert label in text
    assert text.count("未执行") >= 5
    assert "全部通过" not in text


def test_the_mode_and_the_assumed_count_are_in_the_header(page):
    text = " ".join(parse(page).text)
    assert "research" in text and "假设" in text


def test_oracle_events_are_labelled_and_observable_ones_are_not(page):
    assert page.count("仿真真值，实机不可见") >= 1
    observable_row = re.search(
        r"<tr[^>]*>(?:(?!</tr>).)*SOS(?:(?!</tr>).)*</tr>", page, re.DOTALL
    ).group(0)
    assert "仿真真值" not in observable_row


def test_waveform_area_says_no_data_instead_of_drawing_something(page):
    assert "未记录" in page
    assert "<canvas" in page


def test_the_time_cursor_is_a_string_tick_not_a_float(page):
    assert "123456789012" in page, "ticks are carried as decimal strings"
    assert "Date.now" not in page, "the page must not read the wall clock"


def test_there_is_exactly_one_time_cursor(page):
    assert len(re.findall(r'id="time-cursor"', page)) == 1


def test_embedded_data_is_valid_json_and_carries_the_event_list(page):
    blob = re.search(
        r'<script type="application/json" id="run-data">(.*?)</script>', page, re.DOTALL
    ).group(1)
    data = json.loads(blob)
    assert data["meta"]["run_id"] == "r1" and len(data["events"]) == 2
    assert all(isinstance(e["tick"], str) for e in data["events"])


def test_text_from_the_data_is_escaped(page):
    evil = Event(
        kind="<script>alert(1)</script>", origin=Origin.SIMULATED, visibility=Visibility.OBSERVABLE,
        derivation=Derivation.INFERRED, clock_domain=0, count=0, tick=0, session_id=1, epoch=0,
    )  # fmt: skip
    html = build_viewer(load_parameters(), [evil], META)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---- run directory --------------------------------------------------------------------------


def test_a_minimal_run_directory_builds_a_page(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps(META), encoding="utf-8")
    (run / "events.jsonl").write_text(EVENT.model_dump_json() + "\n", encoding="utf-8")
    out = build_viewer_from_run_dir(run, tmp_path / "view.html")
    assert out.is_file() and "SOS" in out.read_text(encoding="utf-8")


def test_a_run_directory_without_events_still_builds_and_says_no_events(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps(META), encoding="utf-8")
    html = build_viewer_from_run_dir(run, tmp_path / "v.html").read_text(encoding="utf-8")
    assert "没有事件" in html


def test_a_missing_run_json_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_viewer_from_run_dir(tmp_path, tmp_path / "v.html")


def test_the_page_is_deterministic(page):
    assert page == build_viewer(load_parameters(), [EVENT, ORACLE], META)


def test_output_path_is_returned_and_written(tmp_path):
    out = build_viewer_from_run_dir(_run(tmp_path), Path(tmp_path) / "x.html")
    assert out.name == "x.html"


def _run(tmp_path):
    run = Path(tmp_path) / "r"
    run.mkdir()
    (run / "run.json").write_text(json.dumps(META), encoding="utf-8")
    return run
