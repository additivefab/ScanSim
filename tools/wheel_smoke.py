"""Run only with the freshly installed wheel Python, isolated from the checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
from http.client import HTTPConnection
from importlib import metadata, resources
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--forbidden-source", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    import zlscan
    from zlscan.api.server import make_server
    from zlscan.engine.run_service import demo_service

    expected = json.loads(args.expected.read_text())
    prefix = Path(sys.prefix).resolve()
    module = Path(zlscan.__file__).resolve()
    assert prefix == args.prefix.resolve() and module.is_relative_to(prefix)
    assert args.forbidden_source.resolve() not in [Path(p).resolve() for p in sys.path if p]
    assert sys.flags.isolated and sys.flags.no_user_site
    assert zlscan.__version__ == metadata.version("zlscan") == expected["version"]
    checked = {}
    for relative, digest in expected["assets"].items():
        payload = resources.files("zlscan").joinpath(*Path(relative).parts[1:]).read_bytes()
        actual = hashlib.sha256(payload).hexdigest()
        assert actual == digest, relative
        checked[relative] = actual
    cli = prefix / ("Scripts/zlscan.exe" if os.name == "nt" else "bin/zlscan")
    commands = []

    def run(arguments: list[str], code: int = 0) -> None:
        p = subprocess.run([str(cli), *arguments], capture_output=True, text=True)
        commands.append(
            dict(arguments=arguments, returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)
        )
        assert p.returncode == code, p.stdout + p.stderr

    run(["--version"])
    assert commands[-1]["stdout"].strip() == f"zlscan {expected['version']}"
    run(["--help"])
    run(["check"], 4)  # Unknown hardware stays unresolved after installation.
    run(["run", "--mode", "digital", "--out", str(args.out / "digital")])
    digital = json.loads((args.out / "digital/run.json").read_text())
    assert digital["integrity"] == "complete" and digital["recon_status"] == "complete"
    scenario = args.out / "research_scenario.json"
    scenario.write_text(
        json.dumps(
            dict(
                format="zlscan.research-scenario/1",
                model_version="chain-0.3",
                preset="nominal",
                lines=4,
            )
        )
    )
    # The nominal preset exceeds its assumed clear-field anode limit; preserve exit 1.
    run(["budget", "--config", str(scenario), "--out", str(args.out / "budget")], 1)
    run(
        [
            "run",
            "--mode",
            "calibration",
            "--preset",
            "nominal",
            "--lines",
            "4",
            "--out",
            str(args.out / "reference"),
        ]
    )
    run(
        [
            "calibrate",
            "--raw",
            str(args.out / "reference/observable/raw.zlraw"),
            "--protocol",
            str(args.out / "reference/observable/scan_protocol.json"),
            "--out",
            str(args.out / "calibration"),
        ]
    )
    run(
        [
            "run",
            "--mode",
            "physical",
            "--config",
            str(scenario),
            "--out",
            str(args.out / "sample"),
        ]
    )
    run(
        [
            "reconstruct",
            "--raw",
            str(args.out / "sample/observable/raw.zlraw"),
            "--capture",
            str(args.out / "sample/observable/capture.json"),
            "--calibration",
            str(args.out / "calibration/calibration.json"),
            "--columns",
            "40",
            "--pitch-x-m",
            "0.002",
            "--pitch-y-m",
            "0.002",
            "--out",
            str(args.out / "reconstructed"),
        ]
    )
    run(
        [
            "report",
            "--run",
            str(args.out / "reconstructed"),
            "--raw",
            str(args.out / "sample/observable/raw.zlraw"),
            "--out",
            str(args.out / "image.html"),
        ]
    )
    assert (args.out / "image.html").is_file()
    budget = json.loads((args.out / "budget/budget.json").read_text())
    sample = json.loads((args.out / "sample/run.json").read_text())
    assert budget["scenario_configuration"]["primary_hash"] == sample["config_hash"]
    from zlscan.engine.saved_service import saved_service

    with saved_service(args.out / "sample") as saved:
        loaded = saved.runs["saved"]
        assert saved.manifest(loaded)["loaded_from_saved"]
        delivered = saved.traces(loaded, "adc.code", 0, loaded.steps, 40, "instrument")
        assert delivered["missing_count"] > 0 and len(delivered["values"]) <= 40
    service = demo_service(lines=2)
    server = make_server(service, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    http_results = {}
    try:
        for path in ["/", "/runs", "/runs/demo/manifest"]:
            conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                payload = response.read()
                assert response.status == 200, (path, response.status)
                if path == "/":
                    assert (
                        hashlib.sha256(payload).hexdigest()
                        == checked["zlscan/api/static/index.html"]
                    )
                elif path.endswith("manifest"):
                    assert json.loads(payload)["origin"] == "SIMULATED"
                else:
                    assert "demo" in json.loads(payload)["runs"]
                http_results[path] = dict(status=response.status, bytes=len(payload))
            finally:
                conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    summary = dict(
        version=zlscan.__version__,
        module_path=str(module),
        prefix=str(prefix),
        python=sys.version,
        platform=platform.platform(),
        process_arch=platform.machine(),
        assets=checked,
        http=http_results,
        commands=commands,
        hardware_check="unresolved",
        scope="same-host isolated wheel smoke only; no browser or hardware acceptance",
    )
    (args.out / "smoke.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            dict(
                version=zlscan.__version__,
                assets=len(checked),
                http="passed",
                cli="passed",
                hardware="unresolved",
                prefix=str(prefix),
            )
        )
    )


if __name__ == "__main__":
    main()
