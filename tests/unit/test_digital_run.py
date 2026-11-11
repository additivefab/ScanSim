"""zlscan run --mode digital: end-to-end digital loop, isolation, workbench checks."""

import json
import re

import numpy as np
import pytest

from zlscan.cli import main
from zlscan.engine.digital_run import run_digital
from zlscan.io.rawstream import read_all


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    out = tmp_path_factory.mktemp("clean")
    return out, run_digital(out)


def test_one_command_produces_a_complete_run_directory(tmp_path):
    assert main(["run", "--mode", "digital", "--out", str(tmp_path / "r")]) == 0
    names = [
        "run.json", "viewer.html", "digital.json", "observable/raw.zlraw",
        "observable/integrity.json", "observable/recon.npz", "truth/source_codes.npy",
    ]  # fmt: skip
    for name in names:
        assert (tmp_path / "r" / name).exists(), name


def test_clean_run_is_complete_and_matches_the_source(clean):
    out, run = clean
    assert run["recon_status"] == "complete" and run["integrity"] == "complete"
    src = np.load(out / "truth" / "source_codes.npy")
    recon = np.load(out / "observable" / "recon.npz")
    lines = read_all(out / "observable" / "raw.zlraw").blocks
    for b in lines:
        row = b.line_number
        want = src[b.first_sample_index : b.first_sample_index + 40, 0]
        assert recon["ch0"][row].tolist() == want.tolist()


def test_reconstruction_only_sees_the_observable_files(clean):
    out, _ = clean
    observable = {p.name for p in (out / "observable").iterdir()}
    assert not any("source" in n or "truth" in n for n in observable)


def test_a_stalled_link_gives_losses_traceable_to_layer_and_range(tmp_path):
    cfg = {"stages": [
        {"name": "rx", "capacity_bytes": 400, "rate_Bps": 1e9},
        {"name": "net", "capacity_bytes": 400, "rate_Bps": 1e5, "stalls": []},
    ]}  # fmt: skip
    run = run_digital(tmp_path, cfg)
    assert run["integrity"] == "incomplete" and run["recon_status"] == "incomplete"
    data = json.loads((tmp_path / "digital.json").read_text(encoding="utf-8"))
    assert data["losses"] and all(x["layer"] and x["first"] <= x["last"] for x in data["losses"])
    page = (tmp_path / "viewer.html").read_text(encoding="utf-8")
    assert "丢样轨道" in page and data["losses"][0]["layer"] in page


def test_runs_are_reproducible(tmp_path):
    a = run_digital(tmp_path / "a")
    b = run_digital(tmp_path / "b")
    assert a == b
    assert (tmp_path / "a/observable/raw.zlraw").read_bytes() == (
        tmp_path / "b/observable/raw.zlraw"
    ).read_bytes()


# ---- workbench acceptance (static checks; no browser is available in this environment) -------


def test_w12_the_page_makes_no_external_request(clean):
    page = (clean[0] / "viewer.html").read_text(encoding="utf-8")
    assert not re.search(r"(src|href)=[\"']https?://", page)
    assert "fetch(" not in page and "XMLHttpRequest" not in page and "import(" not in page


def test_w13_layout_is_fluid(clean):
    page = (clean[0] / "viewer.html").read_text(encoding="utf-8")
    assert "@media (max-width:900px)" in page and "min-width:0" in page
    assert not re.search(r"width:\s*\d{4,}px", page)


def test_w03_four_timestamps_are_shown_separately_in_order(clean):
    data = json.loads((clean[0] / "digital.json").read_text(encoding="utf-8"))
    for s in data["lineage"]:
        assert s["sample_time"] < s["adc_output_time"] < s["pin_arrival_time"] <= s["capture_time"]
    page = (clean[0] / "viewer.html").read_text(encoding="utf-8")
    for label in ("采样时刻", "ADC 输出", "引脚到达", "捕获"):
        assert label in page


def test_w01_w02_there_is_one_cursor_shared_by_all_tracks(clean):
    page = (clean[0] / "viewer.html").read_text(encoding="utf-8")
    assert page.count('id="dig-cursor"') == 1


def test_w04_missing_data_is_marked_not_drawn():
    from zlscan.report.digital_view import build_digital_section

    html = build_digital_section({"tracks": [{"id": "x", "name": "空轨", "kind": "analog",
                                              "values": []}], "times": []})  # fmt: skip
    assert "未记录" in html and 'id="trk-x"' not in html


def test_the_digital_section_shows_origin_and_evidence(clean):
    page = (clean[0] / "viewer.html").read_text(encoding="utf-8")
    assert "SIMULATED" in page and "不是硬件事实" in page
