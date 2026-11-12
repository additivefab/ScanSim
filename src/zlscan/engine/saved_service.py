"""Read-only query registration from saved single-channel physical simulation artifacts."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import numpy as np

from zlscan.api.server import Run, Service
from zlscan.contracts.calibration import Binding
from zlscan.engine.chain import Chain
from zlscan.engine.config_bundle import configuration_for_run
from zlscan.engine.physical_run import simulation_binding
from zlscan.io.manifest import sha256_file
from zlscan.io.saved_adc import SavedADC
from zlscan.replay.replay import CheckpointStore


@contextmanager
def saved_service(directory: Path) -> Iterator[Service]:
    directory = directory.resolve()
    document = json.loads((directory / "run.json").read_text())
    if document.get("mode") != "physical" or not isinstance(document.get("fine_steps"), int):
        raise ValueError("saved workbench supports single-channel physical runs")
    marker = document.get("workbench")
    with ExitStack() as stack:
        cfg = stack.enter_context(configuration_for_run(directory, document))
        codepath = directory / "truth/source_codes.npy"
        if marker and sha256_file(codepath) != marker["source_codes_sha256"]:
            raise ValueError("source ADC hash mismatch")
        codes = np.load(codepath, mmap_mode="r", allow_pickle=False)
        stack.callback(codes._mmap.close)
        if codes.dtype != np.dtype("uint16") or codes.ndim != 2 or codes.shape[1] != 1:
            raise ValueError("unsupported source ADC shape/type")
        steps = document["fine_steps"]
        if steps < 1 or len(codes) != -(-steps // cfg.os):
            raise ValueError("source code count and run duration differ")
        raw = directory / "observable/raw.zlraw"
        if marker:
            for name in ("capture.json", "losses.json"):
                if (
                    sha256_file(directory / "observable" / name)
                    != marker["observable_sha256"][name]
                ):
                    raise ValueError("observable metadata hash mismatch")
        capture = json.loads((directory / "observable/capture.json").read_text())
        if Binding.model_validate(capture["binding"]) != simulation_binding(cfg):
            raise ValueError("capture binding does not match saved configuration")
        if sha256_file(raw) != capture["raw_sha256"]:
            raise ValueError("saved capture SHA mismatch")
        delivered = SavedADC(raw, len(codes), cfg.domain_id)
        clocks = delivered.session.clock_domains
        expected_clock = {
            "id": cfg.domain_id,
            "freq_num": cfg.fs_hz,
            "freq_den": 1,
            "phase_ticks": 0,
        }
        if clocks != [expected_clock] or delivered.session.bits_per_sample != cfg.adc.bits:
            raise ValueError("saved capture clock/ADC metadata mismatch")
        watched = [codepath, *(directory / "truth/workbench").glob("scene_*.npy")]
        identities = {
            p: (p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in watched
        }

        def unchanged() -> None:
            for p, identity in identities.items():
                st = p.stat()
                if identity != (st.st_size, st.st_mtime_ns, st.st_ctime_ns):
                    raise ValueError("saved mmap artifact changed after registration")
            delivered.unchanged()

        losses = json.loads((directory / "observable/losses.json").read_text())
        losses = [
            {
                "first": x["first_sample_index"],
                "last": x["last_sample_index"],
                "layer": x["layer"],
                "reason": x["reason"],
            }
            for x in losses
        ]
        checkpoints = {}
        for path in (directory / "truth/checkpoints").glob("*.json"):
            if not path.stem.isdecimal():
                raise ValueError("invalid checkpoint name")
            step = int(path.stem)
            if marker and marker["checkpoints"].get(path.stem) != sha256_file(path):
                raise ValueError("checkpoint hash mismatch")
            blob = path.read_bytes()
            if json.loads(blob).get("step") != step or not 0 <= step <= steps:
                raise ValueError("checkpoint position mismatch")
            Chain(cfg).restore(blob)  # Validate state/schema; never advance or manufacture a link.
            checkpoints[step] = blob
        if marker and set(marker["checkpoints"]) != {str(s) for s in checkpoints}:
            raise ValueError("saved checkpoint set differs from manifest")
        intervals = np.diff(sorted(checkpoints))
        interval = int(intervals.min()) if len(intervals) else 0
        store = CheckpointStore(interval, checkpoints)
        link_records = {}
        if marker:
            file = directory / "truth/link_lineage.json"
            if sha256_file(file) != marker["link_lineage_sha256"]:
                raise ValueError("saved lineage hash mismatch")
            linked = json.loads(file.read_text())
            if linked.get("visibility") != "ORACLE_ONLY" or linked.get("origin") != "SIMULATED":
                raise ValueError("saved lineage is not a simulated reference")
            link_records = {int(x["sample_index"]): x for x in linked["records"]}
        meta = dict(
            saved_source=str(directory),
            saved_link_records=link_records,
            physical_applicability=document.get("physical_applicability"),
            delivered_adc=delivered,
            validate_saved=unchanged,
            artifact_integrity="fingerprinted" if marker else "legacy_unfingerprinted",
        )
        run = Run(
            "saved",
            cfg,
            steps,
            codes.reshape(-1),
            store,
            {},
            set(),
            {},
            meta=meta,
            losses=losses,
            session_id=delivered.session.session_id,
            epoch=0,
        )
        service = Service()
        service.add(run)
        yield service
