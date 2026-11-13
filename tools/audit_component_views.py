"""Real HTTP component frames, independent controlled Chain and fresh-process byte equality."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import numpy as np

from zlscan.api.server import make_server
from zlscan.engine.chain import Chain
from zlscan.engine.saved_service import saved_service
from zlscan.io.manifest import sha256_file
from zlscan.telemetry.components import COMPONENT_PROBES


def hashes(path):
    return {p.relative_to(path).as_posix(): sha256_file(p) for p in path.rglob("*") if p.is_file()}


def query(root, verify=False):
    source = root / "base/source"
    before = hashes(source)
    assert before == json.loads((root / "base/source_manifest.json").read_text())
    responses = []
    with saved_service(source) as service:
        expected = {}
        for channel, cfg in service.snapshot.configs.items():
            chain = Chain(replace(cfg))
            chain.probe_names = COMPONENT_PROBES
            first = chain.advance(113)
            if channel == 0:
                chain.cfg.detector = replace(chain.cfg.detector, gain=2400.0)
            last = chain.advance(674)
            assert chain.checkpoint() == service.snapshot.current.chains[channel].checkpoint()
            expected[channel] = dict(
                codes=np.concatenate([first.codes, last.codes]),
                **{p: np.concatenate([first.probes[p], last.probes[p]]) for p in COMPONENT_PROBES},
            )
        server = make_server(service, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def get(path, status=200):
            connection = http.client.HTTPConnection(*server.server_address, timeout=30)
            connection.request("GET", path)
            response = connection.getresponse()
            raw = response.read()
            connection.close()
            assert response.status == status, raw
            responses.append(
                dict(
                    path=path,
                    status=status,
                    body_utf8=raw.decode(),
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
            )
            return json.loads(raw) if path != "/" else raw.decode()

        try:
            page = get("/")
            assert 'id="optical-scene"' in page and 'id="component-nodes"' in page
            frames = 0
            for channel in (0, 1):
                prefix = f"/runs/recording-c{channel}"
                assert get(prefix + "/manifest")["component_view_available"]
                signals = get(prefix + "/signals")["signals"]
                assert {s["signal_id"] for s in signals} >= {
                    "source.power_w",
                    "scan.y_m",
                    "scan.facet",
                }
                for step in (0, 7, 112, 113, 115, 336, 337, 340, 786):
                    frame = get(prefix + f"/components?step={step}&req={step}")
                    frames += 1
                    assert frame["tick_ps"] == str(step * 250000)
                    assert frame["visibility"] == "ORACLE_ONLY"
                    data = expected[channel]
                    for key in ("x_m", "y_m", "film_width_m", "film_height_m"):
                        assert frame["geometry"][key] == data[key][step]
                    assert frame["geometry"]["facet"] == int(data["facet"][step])
                    assert frame["geometry"]["laser_gate"] == bool(data["laser_gate"][step])
                    nodes = {n["id"]: n for n in frame["nodes"]}
                    for key, probe in (
                        ("laser", "laser_power_w"),
                        ("film", "t_eff"),
                        ("collect", "power_w"),
                        ("pmt", "current_a"),
                        ("afe", "v_adc"),
                    ):
                        assert nodes[key]["value"] == data[probe][step]
                    adc = nodes["adc"]
                    assert adc["value"] == int(data["codes"][step // 4])
                    assert adc["sample_tick_ps"] == str(step // 4 * 1_000_000)
                    assert adc["held"] == bool(step % 4)
                    if step == 115:
                        assert frame["epoch"] == 1 and adc["epoch"] == 0
                assert (
                    get(prefix + "/components?step=115&view=instrument")["status"] == "not_recorded"
                )
                get(prefix + "/components?step=-1", 400)
                get(prefix + "/components?step=787", 400)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
    assert hashes(source) == before
    if verify:
        (root / "readback_responses.json").write_text(json.dumps(responses, indent=2) + "\n")
        assert responses == json.loads((root / "responses.json").read_text())
    else:
        (root / "responses.json").write_text(json.dumps(responses, indent=2) + "\n")
    return dict(
        origin="SIMULATED",
        visibility="ORACLE_ONLY",
        source_files=len(before),
        component_frames=frames,
        http_responses=len(responses),
        independent_chain_match=True,
        source_unchanged=True,
        scope="787 fine steps, two channels; no browser/hardware acceptance",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = args.out.resolve()
    if args.verify:
        assert query(root, True) == json.loads((root / "audit.json").read_text())
        print("fresh-process component HTTP bytes match")
        return
    root.mkdir(parents=True, exist_ok=False)
    argv = [
        sys.executable,
        str(Path(__file__).with_name("audit_digital_cursor.py")),
        "--out",
        str(root / "base"),
    ]
    result = subprocess.run(argv, capture_output=True, text=True)
    (root / "producer.stdout").write_text(result.stdout)
    (root / "producer.stderr").write_text(result.stderr)
    (root / "producer_receipt.json").write_text(
        json.dumps(dict(argv=argv, returncode=result.returncode))
    )
    assert result.returncode == 0, result.stderr
    (root / "audit.json").write_text(json.dumps(query(root), indent=2) + "\n")
    print("component frames and independent controlled Chain match")


if __name__ == "__main__":
    main()
