"""Query service and second-level page (W01-W11 as far as they can be checked without a browser)."""

import http.client
import json
import re
import threading
from pathlib import Path

import numpy as np
import pytest

from zlscan.api.server import STATIC, make_server
from zlscan.engine.run_service import demo_service


@pytest.fixture(scope="module")
def server():
    svc = demo_service(lines=6)
    srv = make_server(svc, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1], svc
    srv.shutdown()
    srv.server_close()


def get(port, path, method="GET", body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request(method, path, body=json.dumps(body) if body is not None else None)
    r = c.getresponse()
    raw = r.read()
    ctype = r.getheader("Content-Type")
    c.close()
    return r.status, (json.loads(raw) if "json" in ctype else raw.decode())


def test_it_refuses_to_bind_to_a_public_address():
    from zlscan.api.server import Service

    with pytest.raises(ValueError):
        make_server(Service(), "0.0.0.0", 0)


def test_http_json_order_is_canonical_even_when_nested_insertion_order_differs():
    from zlscan.api.server import Service

    service = Service()
    service.runs["r"] = object()
    replies = [dict(b=2, a=dict(y=2, x=1)), dict(a=dict(x=1, y=2), b=2)]
    service.manifest = lambda run: replies.pop(0)
    server = make_server(service, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        bodies = []
        for _ in range(2):
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            connection.request("GET", "/runs/r/manifest")
            response = connection.getresponse()
            assert response.status == 200
            bodies.append(response.read())
            connection.close()
        assert bodies[0] == bodies[1]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_manifest_states_origin_evidence_and_step(server):
    port, _ = server
    code, m = get(port, "/runs/demo/manifest?req=1")
    assert code == 200 and m["origin"] == "SIMULATED" and m["evidence"]
    assert m["req"] == "1" and m["status"] == "exact_record"


def test_w01_all_views_use_the_same_run_epoch_and_step(server):
    port, _ = server
    _, m = get(port, "/runs/demo/manifest")
    _, st = get(port, "/runs/demo/state?step=5000&probes=power_w,v_adc")
    assert st["run_id"] == m["run_id"] and st["epoch"] == m["epoch"] and st["step"] == 5000
    assert set(st["values"]) == {"pmt.power_w", "afe.v_adc"}


def test_components_use_the_same_demo_cursor_and_instrument_cannot_access_them(server):
    port, _ = server
    code, frame = get(port, "/runs/demo/components?step=7")
    assert code == 200 and frame["step"] == "7" and frame["run_id"] == "demo"
    adc = next(n for n in frame["nodes"] if n["id"] == "adc")
    assert adc["sample_index"] == "1" and adc["held"]
    assert int(adc["sample_tick_ps"]) < int(frame["tick_ps"])
    assert get(port, "/runs/demo/components?step=7&view=instrument")[1]["status"] == "not_recorded"
    assert get(port, "/runs/demo/components?step=-1")[0] == 400


def test_w02_repeated_queries_return_the_same_record(server):
    port, _ = server
    a = get(port, "/runs/demo/state?step=7777&probes=current_a")[1]
    b = get(port, "/runs/demo/state?step=7777&probes=current_a")[1]
    a.pop("req"), b.pop("req")
    assert a == b


def test_w03_lineage_has_four_distinct_ordered_timestamps(server):
    port, _ = server
    _, lin = get(port, "/runs/demo/samples/100/lineage")
    x = lin["lineage"]
    assert (
        int(x["sample_time"])
        < int(x["adc_output_time"])
        < int(x["pin_arrival_time"])
        <= int(x["capture_time"])
    )
    assert x["sample_index"] == "100"
    assert x["host_arrival_time"] is None


def test_w04_an_unrecorded_signal_is_marked_not_drawn(server):
    port, svc = server
    run = svc.runs["demo"]
    saved = run.pyramids.pop("power_w")
    try:
        _, b = get(port, "/runs/demo/traces?signal=pmt.power_w&start=0&end=100&pixel_width=800")
        assert b["status"] == "not_recorded" and "values" not in b
    finally:
        run.pyramids["power_w"] = saved
    _, img = get(port, "/runs/demo/images/x/tiles/0")
    assert img["status"] == "not_computed"


def test_w05_asking_for_probes_does_not_change_the_codes(server):
    port, _ = server
    a = get(port, "/runs/demo/traces?signal=adc.code&start=0&end=4000&pixel_width=800")[1]
    get(port, "/runs/demo/state?step=100&probes=v_adc,power_w,current_a")
    b = get(port, "/runs/demo/traces?signal=adc.code&start=0&end=4000&pixel_width=800")[1]
    assert a["values"] == b["values"]


def test_w06_the_instrument_view_hides_oracle_signals(server):
    port, _ = server
    _, research = get(port, "/runs/demo/signals?view=research")
    _, instr = get(port, "/runs/demo/signals?view=instrument")
    assert len(research["signals"]) > len(instr["signals"])
    assert {s["signal_id"] for s in instr["signals"]} == {"adc.code"}
    assert all(
        s["visibility"] == "ORACLE_ONLY"
        for s in research["signals"]
        if s["signal_id"] != "adc.code"
    )


def test_w06_the_instrument_view_cannot_read_truth_state(server):
    port, _ = server
    _, b = get(port, "/runs/demo/state?step=20000&probes=power_w&view=instrument")
    assert b["status"] == "not_recorded" and "values" not in b


def test_w09_a_coarse_view_still_shows_the_injected_pulse(server):
    port, svc = server
    steps = svc.runs["demo"].steps
    _, b = get(port, f"/runs/demo/traces?signal=pmt.power_w&start=0&end={steps}&pixel_width=100")
    assert b["status"] == "summary_only"
    peak = max(row[1] for row in b["values"])
    median = float(np.median([row[1] for row in b["values"]]))
    assert peak > 10 * median, "the one-step pulse is still the maximum of its block"


def test_w10_replay_is_reproducible_and_a_branch_gets_a_new_id(server):
    port, svc = server
    body = {"start": 12345, "n_steps": 400, "probes": ["v_adc"]}
    a = get(port, "/runs/demo/replay-window", "POST", body)[1]
    b = get(port, "/runs/demo/replay-window", "POST", body)[1]
    assert a["status"] == "replayed" and a["codes"] == b["codes"] and a["probes"] == b["probes"]
    n_before = len(svc.runs)
    _, br = get(port, "/runs/demo/branches", "POST", {"gain": 1100})
    assert br["status"] == "exact_record" and br["run_id"] != "demo" and br["parent"] == "demo"
    assert len(svc.runs) == n_before + 1
    # the original run is untouched by the branch
    again = get(port, "/runs/demo/traces?signal=adc.code&start=0&end=400&pixel_width=800")[1]
    assert again["values"] == svc.runs["demo"].codes[:100].tolist()


def test_w11_every_answer_echoes_the_request_number_for_stale_dropping(server):
    port, _ = server
    for n in (3, 9):
        assert get(port, f"/runs/demo/manifest?req={n}")[1]["req"] == str(n)
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "lastApplied" in html and "stale(" in html


def test_unknown_things_are_404_and_bad_numbers_are_400(server):
    port, _ = server
    assert get(port, "/runs/nope/manifest")[0] == 404
    assert get(port, "/runs/demo/state?step=abc")[0] == 400
    assert get(port, "/runs/demo/updates")[0] == 501


def test_the_page_is_served_and_makes_no_external_request(server):
    port, _ = server
    code, page = get(port, "/")
    assert code == 200 and "研究视图" in page and "实机可观测视图" in page
    assert not re.search(r"(src|href)=[\"']https?://", page)
    assert "未记录" in page and "功能示意" in page


def test_w13_the_page_layout_is_fluid():
    html = Path(STATIC / "index.html").read_text(encoding="utf-8")
    assert "@media (max-width:900px)" in html and "min-width:0" in html
    fixed_width = r"(?<![-\w])width:\s*\d{4,}px"
    assert re.search(fixed_width, "width:1200px")
    assert not re.search(fixed_width, "@media(max-width:1000px)")
    assert not re.search(fixed_width, html)


def test_w08_a_stalled_link_shows_up_with_layer_and_sample_range(server):
    port, svc = server
    steps = svc.runs["demo"].steps
    _, b = get(port, f"/runs/demo/events?start=0&end={steps}&kinds=transport_loss")
    losses = [e for e in b["events"] if e["kind"] == "transport_loss"]
    assert losses and all(e["layer"] and e["first"] <= e["last"] for e in losses)
    run = svc.runs["demo"]
    assert {(e["first"], e["last"]) for e in losses} <= {
        (x["first"], x["last"]) for x in run.losses
    }


def test_instrument_queries_refuse_private_trace_and_replay_probes(server):
    port, _ = server
    _, trace = get(port, "/runs/demo/traces?signal=pmt.power_w&start=0&end=100&view=instrument")
    assert trace["status"] == "not_recorded"
    assert "values" not in trace
    _, replay = get(
        port,
        "/runs/demo/replay-window?view=instrument",
        "POST",
        {"start": 0, "n_steps": 100, "probes": ["v_adc"]},
    )
    assert replay["codes"]
    assert replay["probes"] == {}
