"""Exercise the real saved-run CLI/HTTP path and verify original file hashes."""

from __future__ import annotations

import argparse
import http.client
import json
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import scan_blocks


def hashes(path: Path) -> dict:
    return {p.relative_to(path).as_posix(): sha256_file(p) for p in path.rglob("*") if p.is_file()}


def verify(root: Path) -> dict:
    source = root / "source"
    before = json.loads((root / "source_manifest.json").read_text())
    assert hashes(source) == before
    log = root / "server.log"
    with log.open("w") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "zlscan", "serve", "--run", str(source), "--port", "0"],
            stdout=stream,
            stderr=stream,
        )
        try:
            deadline = time.monotonic() + 30
            port = None
            while time.monotonic() < deadline:
                match = re.search(r"http://127\.0\.0\.1:(\d+)/", log.read_text())
                if match:
                    port = int(match.group(1))
                    break
                if process.poll() is not None:
                    raise RuntimeError("saved server exited during startup; see server.log")
                time.sleep(0.05)
            assert port is not None, "saved server startup timed out"

            def query(path: str, body: dict | None = None) -> dict:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                try:
                    data = json.dumps(body) if body is not None else None
                    connection.request("POST" if body is not None else "GET", path, body=data)
                    response = connection.getresponse()
                    assert response.status == 200, (path, response.status)
                    return json.loads(response.read())
                finally:
                    connection.close()

            manifest = query("/runs/saved/manifest")
            assert manifest["loaded_from_saved"] and manifest["read_only"]
            span = manifest["steps"]
            observable = query(
                f"/runs/saved/traces?signal=adc.code&start=0&end={span}&pixel_width=40&view=instrument"
            )
            research = query(
                f"/runs/saved/traces?signal=adc.code&start=0&end={span}&pixel_width=40&view=research"
            )
            assert (
                observable["visibility"] == "OBSERVABLE" and research["visibility"] == "ORACLE_ONLY"
            )
            assert len(observable["values"]) <= 40 and observable["missing_count"] > 0
            raw = scan_blocks(source / "observable/raw.zlraw", lambda *_: None)
            assert sum(observable["recorded_counts"]) == sum(b.sample_count for b in raw.blocks)
            state = query("/runs/saved/state?step=11000&probes=power_w&view=research")
            assert state["status"] == "replayed"
            replay = query(
                "/runs/saved/replay-window?view=research",
                dict(start=11000, n_steps=100, probes=["power_w"]),
            )
            codes = np.load(source / "truth/source_codes.npy", mmap_mode="r", allow_pickle=False)
            try:
                lo = replay["first_sample"]
                assert replay["codes"] == codes.reshape(-1)[lo : lo + len(replay["codes"])].tolist()
            finally:
                codes._mmap.close()
            assert (
                query("/runs/saved/replay-window?view=instrument", dict(start=11000, n_steps=100))[
                    "status"
                ]
                == "not_computed"
            )
            record = json.loads((source / "truth/link_lineage.json").read_text())["records"][0]
            i = record["sample_index"]
            lineage = query(f"/runs/saved/samples/{i}/lineage?view=research")
            assert lineage["lineage"]["adc_output_time"] == str(record["adc_output_time"])
            assert lineage["lineage"]["host_arrival_time"] is None
            assert (
                query(f"/runs/saved/samples/{i}/lineage?view=instrument")["status"]
                == "not_recorded"
            )
            result = dict(
                manifest=manifest,
                observable=observable,
                research=research,
                state=state,
                replay=replay,
                lineage=lineage,
                source_file_count=len(before),
            )
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            code = process.wait(timeout=20)
            assert code in (0, -signal.SIGINT, 130), code
    assert hashes(source) == before
    print("Saved CLI/HTTP queries and checkpoint replay passed; original files unchanged")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path)
    group.add_argument("--verify", type=Path)
    args = p.parse_args()
    if args.out:
        root = args.out.resolve()
        root.mkdir(parents=True, exist_ok=False)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "zlscan",
                "run",
                "--mode",
                "physical",
                "--lines",
                "3",
                "--out",
                str(root / "source"),
            ],
            capture_output=True,
            text=True,
        )
        (root / "producer.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stderr
        (root / "source_manifest.json").write_text(
            json.dumps(hashes(root / "source"), indent=2) + "\n"
        )
        report = verify(root)
        (root / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        expected = json.loads((args.verify / "audit.json").read_text())
        assert verify(args.verify.resolve()) == expected, "fresh-process saved queries differ"
        print("Fresh-process saved query results agree exactly")


if __name__ == "__main__":
    main()
