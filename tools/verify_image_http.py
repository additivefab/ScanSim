"""Exercise real HTTP image/source queries; no browser or physical-model acceptance claim."""

from __future__ import annotations

import argparse
import http.client
import json
import threading
from pathlib import Path

from zlscan.api.server import make_server
from zlscan.engine.run_service import image_service
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import scan_blocks


def verify(registry: Path) -> dict:
    result = {"scope": "HTTP image/source consistency; excludes browser and physical validity"}
    registered = json.loads(registry.read_text())["runs"]
    rows = []
    with image_service(registry) as service:
        server = make_server(service)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def query(run_id, route):
            connection = http.client.HTTPConnection(*server.server_address, timeout=30)
            try:
                connection.request("GET", f"/runs/{run_id}/{route}")
                response = connection.getresponse()
                value = json.loads(response.read())
                assert response.status == 200, value
                return value
            finally:
                connection.close()

        try:
            for entry in registered:
                run_id = entry["id"]
                raw = (registry.parent / entry["raw"]).resolve()
                reconstruction = (registry.parent / entry["reconstruction"]).resolve()
                paths = [
                    raw,
                    *(reconstruction / n for n in ("run.json", "density.npz", "lineage.json")),
                ]
                before = {str(path): sha256_file(path) for path in paths}
                manifest = query(run_id, "manifest")
                image = query(run_id, "images/density/manifest")
                tile = query(run_id, "images/density/tiles/0/0/0?req=7")
                assert tile["width"] <= 256 and tile["height"] <= 256
                assert tile["req"] == "7"
                row = {"run_id": run_id, "image": image, "source_hashes": before}
                if image["calibration_status"] == "CALIBRATION_MISMATCH":
                    assert all(v is None for line in tile["density"] for v in line)
                    assert (
                        query(run_id, "images/density/pixels/0/0/lineage")["status"]
                        == "not_computed"
                    )
                    row["mismatch_hidden"] = True
                else:
                    candidates = [
                        (r, c)
                        for r, line in enumerate(tile["flags"])
                        for c, flag in enumerate(line)
                        if flag == 0
                    ]
                    assert candidates, "verification requires a valid pixel in the first tile"
                    r, c = candidates[len(candidates) // 2]
                    pixel = query(run_id, f"images/density/pixels/{r}/{c}/lineage?view=instrument")
                    assert abs(sum(s["normalized_weight"] for s in pixel["samples"]) - 1) < 1e-10
                    sample = pixel["samples"][0]
                    absolute = int(sample["sample_index"])
                    offset = absolute - int(manifest["sample_origin"])
                    state = query(run_id, f"state?step={offset}")
                    trace = query(
                        run_id,
                        f"traces?signal=adc.code&start={offset}&end={offset + 1}&pixel_width=1",
                    )
                    assert state["sample_index"] == sample["sample_index"]
                    assert state["sample_time_ticks"] == sample["sample_time_ticks"]
                    independently_read = []

                    def consume(
                        session,
                        block,
                        codes,
                        selected=sample,
                        absolute_index=absolute,
                        found=independently_read,
                    ):
                        index = absolute_index - block.first_sample_index
                        if (
                            block.epoch == selected["epoch"]
                            and block.clock_domain == selected["clock_domain"]
                            and selected["channel"] in block.channels
                            and 0 <= index < block.sample_count
                        ):
                            found.append(
                                int(codes[index, block.channels.index(selected["channel"])])
                            )
                            clock = next(
                                d
                                for d in session.clock_domains
                                if d["id"] == selected["clock_domain"]
                            )
                            tick = absolute_index * 10**12 * clock.get("freq_den", 1) // clock[
                                "freq_num"
                            ] + clock.get("phase_ticks", 0)
                            assert selected["sample_time_ticks"] == str(tick)

                    scan_blocks(raw, consume)
                    assert independently_read == trace["values"]
                    summary = query(
                        run_id,
                        f"traces?signal=adc.code&start=0&end={manifest['steps_count']}&pixel_width=31",
                    )
                    assert len(summary["values"]) <= 62
                    row.update(
                        pixel=pixel,
                        selected_raw_code=trace["values"][0],
                        overview_rows=len(summary["values"]),
                        missing_count=summary["missing_count"],
                    )
                assert before == {str(path): sha256_file(path) for path in paths}
                rows.append(row)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
    result["runs"] = rows
    result["passed"] = True
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.registry)
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(f"HTTP image/source checks passed for {len(report['runs'])} registered runs")
