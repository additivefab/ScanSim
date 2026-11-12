import http.client
import json
import threading

import numpy as np
import pytest

from zlscan.api.server import ImageRun, Service, make_server
from zlscan.contracts.calibration import Binding, CalibItem, CalibrationHeader, CaptureDescription
from zlscan.io.image_artifact import ImageArtifact
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import FLAG_LINE_START, RawWriter, SessionHeader
from zlscan.recon.capture import reconstruct_capture
from zlscan.recon.density import Grid


def capture_image(tmp_path, *, mapped=False, sample_origin=10, width=3, line_spacing=20):
    raw = tmp_path / "raw.zlraw"
    session = SessionHeader(
        schema_version="0.1",
        session_id=9,
        created_at="fixture",
        hardware_hash=None,
        firmware_hash=None,
        wiring_rev=None,
        register_dump_hash=None,
        calibration_id=None,
        channel_roles={"0": None},
        clock_domains=[{"id": 1, "freq_num": 1_000_000}],
        coding="offset_binary",
        bits_per_sample=16,
        bytes_per_sample=2,
        windows={"window_length": 4, "window_start": 0, "sync_latency_cycles": 0},
        origin="SIMULATED",
    )
    with RawWriter(raw, session) as writer:
        for line in range(3):
            writer.write_block(
                epoch=0,
                channel_mask=1,
                clock_domain=1,
                first_sample_index=sample_origin + line_spacing * line,
                samples=np.full((4, 1), 100 + line * 20, np.uint16),
                line_number=line,
                flags=FLAG_LINE_START,
            )
    binding = Binding(simulation_hash="fixture", channel_roles={"0": None})
    capture = CaptureDescription(raw_sha256=sha256_file(raw), binding=binding, lines=3)
    cal = CalibrationHeader(
        format_version=1,
        provenance="SIMULATED",
        calibration_id="cal-1",
        created_at="fixture",
        binds_to=binding,
        items={
            "transfer_lut": CalibItem(
                value={
                    "channels": {
                        "0": {"code_knots": [0, 100, 200], "intensity_knots": [0.01, 0.1, 1.0]}
                    }
                }
            ),
            "dark_raw_code": CalibItem(value={"0": {"median": [0] * 4}}),
            "white_raw_code": CalibItem(value={"0": {"median": [200] * 4}}),
        },
    )
    facets = None
    if mapped:
        policy = {
            "facets": 2,
            "period_ticks": 20_000_000,
            "tolerance_ticks": 0,
            "phase_reference": "fixture/phase",
        }
        cal.items["facet_geometry"] = CalibItem(
            value={
                "policy": policy,
                "window": session.windows,
                "facets": {
                    str(i): {
                        "x_coefficients_m": [1.5, 1.5, 0.2],
                        "x_residual_rms_m": 0,
                        "support_u": [-1, 1],
                        "y_offset_m": 0.3 if i == 0 else -0.2,
                    }
                    for i in range(2)
                },
            }
        )
        syncs = []
        for line in range(3):
            event = {
                "kind": "line_sync",
                "origin": "SIMULATED",
                "visibility": "OBSERVABLE",
                "derivation": "SENSOR",
                "capability_ref": "fixture/sync",
                "clock_domain": 1,
                "count": 10 + 20 * line,
                "tick": (10 + 20 * line) * 1_000_000,
                "session_id": 9,
                "epoch": 0,
            }
            syncs.append(
                {
                    "line": line,
                    "sync": event,
                    "anchor": {
                        **event,
                        "kind": "facet_index",
                        "capability_ref": "fixture/phase",
                        "payload": {"phase": line % 2},
                    }
                    if line == 0
                    else None,
                }
            )
        facets = tmp_path / "facets.json"
        facets.write_text(
            json.dumps({"raw_sha256": capture.raw_sha256, "policy": policy, "lines": syncs})
        )
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, cal, Grid(width, 3, 1, 1), output, facet_path=facets)
    return output, raw


@pytest.mark.parametrize("mapped", [False, True])
def test_pixel_lineage_reproduces_saved_linear_domain_image(tmp_path, mapped):
    directory, raw = capture_image(tmp_path, mapped=mapped)
    with ImageArtifact(directory, raw_path=raw) as image:
        with np.load(directory / "density.npz") as arrays:
            for row, col in zip(*np.where(arrays["flags"] == 0), strict=True):
                lineage = image.pixel_lineage(int(row), int(col))
                samples = lineage["samples"]
                assert sum(s["normalized_weight"] for s in samples) == pytest.approx(1)
                expected = sum(
                    s["normalized_weight"] * ((0.1 + s["line"] * 0.18 - 0.01) / 0.99)
                    for s in samples
                )
                assert arrays["transmittance"][row, col] == pytest.approx(expected)
                assert all(
                    int(s["sample_time_ticks"]) == int(s["sample_index"]) * 1_000_000
                    for s in samples
                )
        if mapped:
            assert len({s["line"] for s in image.pixel_lineage(1, 1)["samples"]}) == 2
        tile = image.tile(0, 0)
        assert tile["width"] == 3 and tile["height"] == 3


def test_calibration_mismatch_is_visible_and_never_exposes_density(tmp_path):
    directory, raw = capture_image(tmp_path)
    with ImageArtifact(directory, raw_path=raw, expected_calibration_id="other") as image:
        manifest = image.manifest()
        assert manifest["calibration_status"] == "CALIBRATION_MISMATCH"
        assert all(v is None for row in image.tile(0, 0)["density"] for v in row)
        assert image.pixel_lineage(1, 1)["status"] == "not_computed"


def test_changed_raw_or_lineage_is_rejected(tmp_path):
    directory, raw = capture_image(tmp_path)
    with ImageArtifact(directory, raw_path=raw) as image:
        with raw.open("ab") as handle:
            handle.write(b"changed")
        with pytest.raises(ValueError, match="changed"):
            image.traces(0, 10)
    with pytest.raises(ValueError, match="SHA-256"):
        ImageArtifact(directory, raw_path=raw)


def test_http_image_queries_preserve_source_and_source_sample_times(tmp_path):
    directory, raw = capture_image(tmp_path, mapped=True)
    before = {p: sha256_file(p) for p in (directory / "density.npz", raw)}
    with ImageArtifact(directory, raw_path=raw) as image:
        service = Service()
        service.add(ImageRun("capture", image))
        server = make_server(service)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:

            def query(path):
                conn = http.client.HTTPConnection(*server.server_address)
                conn.request("GET", "/runs/capture/" + path)
                response = conn.getresponse()
                body = json.loads(response.read())
                conn.close()
                assert response.status == 200
                return body

            assert query("manifest")["cursor_unit"] == "sample"
            assert query("images/density/tiles/0/0/0?req=4")["req"] == "4"
            pixel = query("images/density/pixels/1/1/lineage?view=instrument")
            assert pixel["samples"] and pixel["status"] == "exact_record"
            first = int(pixel["samples"][0]["sample_index"])
            state = query(f"state?step={first - image.first_sample}")
            assert state["sample_time_ticks"] == str(first * 1_000_000)
            trace = query("traces?signal=adc.code&start=0&end=5&pixel_width=100")
            assert trace["values"] == [100, 100, 100, 100, None]
        finally:
            server.shutdown()
            server.server_close()
    assert {p: sha256_file(p) for p in before} == before


def test_capture_trace_summary_is_bounded_and_preserves_gaps(tmp_path):
    directory, raw = capture_image(tmp_path)
    with ImageArtifact(directory, raw_path=raw) as image:
        trace = image.traces(0, image.span, 2)
        assert trace["status"] == "summary_only"
        assert len(trace["values"]) == 2
        assert sum(row[4] for row in trace["values"]) == image.span
        assert sum(trace["recorded_counts"]) == 12
        assert trace["missing_count"] == image.span - 12
        assert max(row[1] for row in trace["values"]) == 140
        assert trace["sample_time"]["first_tick_ps"] == "10000000"
        assert image.traces(-10, 1, 2)["values"] == [100]
        assert image.traces(image.span, image.span + 10, 2)["status"] == "not_computed"
        with pytest.raises(ValueError):
            image.traces(0, image.span, 0)


def test_capture_registry_loads_two_independent_runs_and_rejects_duplicate_ids(tmp_path):
    from zlscan.engine.run_service import image_service

    directory, raw = capture_image(tmp_path)
    registry = tmp_path / "registry.json"
    entry = {"id": "a", "reconstruction": "image", "raw": raw.name}
    registry.write_text(json.dumps({"runs": [entry, {**entry, "id": "b"}]}))
    with image_service(registry) as service:
        assert list(service.runs) == ["a", "b"]
        assert service.manifest(service.runs["a"])["cursor_unit"] == "sample"
        assert service.manifest(service.runs["b"])["sample_origin"] == "10"
    registry.write_text(json.dumps({"runs": [entry, entry]}))
    with pytest.raises(ValueError, match="unique"):
        with image_service(registry):
            pass


def test_capture_binding_mismatch_masks_images(tmp_path):
    directory, raw = capture_image(tmp_path)
    with ImageArtifact(
        directory,
        raw_path=raw,
        expected_binding={"simulation_hash": "different", "channel_roles": {"0": None}},
    ) as image:
        assert image.manifest()["calibration_status"] == "CALIBRATION_MISMATCH"
        assert image.pixel_lineage(1, 1)["status"] == "not_computed"
        assert all(value is None for row in image.tile(0, 0)["density"] for value in row)


def test_large_capture_sample_identity_survives_json_roundtrip(tmp_path):
    origin = 2**53 + 17
    directory, raw = capture_image(tmp_path, sample_origin=origin)
    with ImageArtifact(directory, raw_path=raw) as image:
        state = json.loads(json.dumps(image.state(1)))
        assert state["sample_index"] == str(origin + 1)
        assert state["sample_time_ticks"] == str((origin + 1) * 1_000_000)
        pixel = image.pixel_lineage(1, 1)
        assert all(isinstance(sample["sample_index"], str) for sample in pixel["samples"])
        assert min(int(sample["sample_index"]) for sample in pixel["samples"]) >= origin


def test_tiles_are_bounded_at_full_resolution_edges(tmp_path):
    directory, raw = capture_image(tmp_path, width=257)
    with ImageArtifact(directory, raw_path=raw) as image:
        assert image.tile(0, 0)["width"] == 256
        assert image.tile(1, 0)["width"] == 1
        for x, y in [(-1, 0), (2, 0), (0, 1)]:
            with pytest.raises(ValueError, match="outside"):
                image.tile(x, y)


def test_large_sparse_trace_keeps_missing_buckets_and_peak(tmp_path):
    directory, raw = capture_image(tmp_path, line_spacing=1_000_003)
    with ImageArtifact(directory, raw_path=raw) as image:
        trace = image.traces(0, image.span, 31)
        assert len(trace["values"]) <= 31
        assert sum(trace["recorded_counts"]) == 12
        assert trace["missing_count"] == image.span - 12
        assert any(row[0] is None for row in trace["values"])
        assert max(row[1] for row in trace["values"] if row[1] is not None) == 140
