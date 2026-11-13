"""Observed geometric landmarks identify combinations; validation never adjusts the model."""

import json

import numpy as np
import pytest

from zlscan.contracts.calibration import Binding, CaptureDescription
from zlscan.ident.geometry import GeometryFitConfig, identify_geometry_files
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import FLAG_LINE_END, FLAG_LINE_START, RawWriter, SessionHeader

COEFFICIENTS = [[0.01, 0.02, 0.001, 0.0003], [0.011, 0.019, -0.001, -0.0002]]


def fixture(root, *, grid_only=False, anchors=True, uniform=False):
    cases = []
    for index, partition in enumerate(["fit", "validation"]):
        raw = root / f"raw-{index}.zlraw"
        session = SessionHeader(
            schema_version="0.1",
            session_id=19 + index,
            created_at="2026-11-13T00:00:00Z",
            hardware_hash=None,
            firmware_hash=None,
            wiring_rev=None,
            register_dump_hash=None,
            calibration_id=None,
            channel_roles={"0": None},
            clock_domains=[dict(id=1, freq_num=1000000, freq_den=1, phase_ticks=0)],
            coding="offset_binary",
            bits_per_sample=16,
            bytes_per_sample=2,
            windows=dict(window_length=100, window_start=0, sync_latency_cycles=0),
            origin="SIMULATED",
        )
        timing, landmarks = [], []
        with RawWriter(raw, session) as writer:
            for line in range(4):
                writer.write_block(
                    epoch=0,
                    channel_mask=1,
                    clock_domain=1,
                    first_sample_index=line * 200,
                    samples=np.full((100, 1), 30000, np.uint16),
                    flags=FLAG_LINE_START | FLAG_LINE_END,
                    line_number=line,
                )
                event = dict(
                    kind="line_sync",
                    origin="SIMULATED",
                    visibility="OBSERVABLE",
                    derivation="SENSOR",
                    clock_domain=1,
                    count=line,
                    tick=line * 200000000,
                    session_id=19 + index,
                    epoch=0,
                    capability_ref="synthetic/sync",
                )
                anchor = (
                    dict(
                        event,
                        kind="facet_index",
                        capability_ref="synthetic/index",
                        payload={"phase": 0},
                    )
                    if line == 0 and anchors
                    else None
                )
                timing.append(dict(line=line, sync=event, anchor=anchor))
                for j, (u, slope) in enumerate(
                    [(u, 0.0) for u in [-0.8, -0.4, 0.0, 0.4, 0.8]]
                    + ([] if grid_only else [(u, 1.5) for u in [-0.6, -0.2, 0.2, 0.6]])
                ):
                    a, b, c, dy = COEFFICIENTS[line % 2]
                    landmarks.append(
                        dict(
                            name=f"l{line}-{j}",
                            line=line,
                            sample_column=(u + 1) * 50 - 0.5,
                            target_x_m=a + b * u + c * u**3 - slope * dy,
                            target_dx_dy=slope,
                            standard_uncertainty_x_m=1e-6,
                            uncertainty_source="synthetic independent known target-x error",
                        )
                    )
        capture = CaptureDescription(
            raw_sha256=sha256_file(raw),
            lines=4,
            binding=Binding(simulation_hash="bench", channel_roles={"0": None}),
        )
        cap = root / f"capture-{index}.json"
        cap.write_text(capture.model_dump_json(), encoding="utf-8")
        doc = dict(
            format="zlscan.geometry-observations/1",
            source_kind="synthetic",
            device="synthetic scanner",
            measurement_point="known target markings",
            settings_source="explicit synthetic instrument",
            extraction_method="synthetic annotation; not pixel extraction",
            target_certificate_ref="synthetic known dimensions",
            acquisition_id=f"acq-{index}",
            raw_path=raw.name,
            raw_sha256=sha256_file(raw),
            capture_path=cap.name,
            capture_sha256=sha256_file(cap),
            channel=0,
            timing=dict(
                raw_sha256=sha256_file(raw),
                policy=dict(
                    facets=2,
                    period_ticks=200000000,
                    tolerance_ticks=1,
                    phase_reference="synthetic/index",
                ),
                lines=timing,
            ),
            landmarks=[] if uniform else landmarks,
        )
        path = root / f"observations-{index}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        cases.append(
            dict(
                name=f"scan-{index}", path=path.name, sha256=sha256_file(path), partition=partition
            )
        )
    return dict(
        format="zlscan.geometry-fit-config/1",
        confidence_level=0.95,
        rank_rtol=1e-10,
        assumption_source="explicit conditional synthetic protocol",
        input_handling="conditional_on_declared_landmarks_and_slopes",
        error_model="independent_gaussian_known_target_x_sigma",
        validation_max_abs_residual_m=1e-5,
        captures=cases,
    )


def run(root, config):
    return identify_geometry_files(GeometryFitConfig.model_validate_json(json.dumps(config)), root)


def edit(root, config, index, update):
    row = config["captures"][index]
    path = root / row["path"]
    document = json.loads(path.read_bytes())
    update(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    row["sha256"] = sha256_file(path)


def test_recovers_facet_distortion_and_feed_offset_with_uncertainty(tmp_path):
    config = fixture(tmp_path)
    result = run(tmp_path, config)
    assert result["status"] == "identified"
    assert result["validation"]["status"] == "compatible"
    for phase, expected in enumerate(COEFFICIENTS):
        fit = result["facets"][str(phase)]
        assert fit["rank"] == 4
        for name, value in zip(fit["coefficient_order"], expected, strict=True):
            p = fit["parameters"][name]
            assert p["value"] == pytest.approx(value, abs=1e-12)
            assert p["conditional_interval"][0] < value < p["conditional_interval"][1]
        assert np.isfinite(fit["conditional_covariance"]).all()
    assert result["parameter_upgrades"] == []
    assert result["hard_acceptance_eligible"] is False


def test_grid_alone_leaves_feed_offset_unidentifiable(tmp_path):
    result = run(tmp_path, fixture(tmp_path, grid_only=True))
    assert result["status"] == "partially_identifiable"
    for fit in result["facets"].values():
        assert fit["rank"] == 3
        assert fit["parameters"]["y_offset_m"]["value"] is None
        assert len(fit["identifiable_combinations"]) == 3


@pytest.mark.parametrize("kind", ["uniform", "no_anchor"])
def test_no_features_or_unobservable_phase_never_gets_invented(tmp_path, kind):
    result = run(
        tmp_path, fixture(tmp_path, uniform=kind == "uniform", anchors=kind != "no_anchor")
    )
    assert result["status"] == "unidentifiable"
    assert all(
        p["value"] is None for f in result["facets"].values() for p in f["parameters"].values()
    )


def test_validation_mismatch_does_not_refit(tmp_path):
    config = fixture(tmp_path)
    before = run(tmp_path, config)

    def shift(doc):
        for p in doc["landmarks"]:
            p["target_x_m"] += 0.005

    edit(tmp_path, config, 1, shift)
    after = run(tmp_path, config)
    for phase in before["facets"]:
        for key in before["facets"][phase]:
            if key != "rows":
                assert before["facets"][phase][key] == after["facets"][phase][key]
        assert [r for r in before["facets"][phase]["rows"] if r["partition"] == "fit"] == [
            r for r in after["facets"][phase]["rows"] if r["partition"] == "fit"
        ]
    assert after["validation"]["status"] == "mismatch"


def test_reused_source_is_rejected(tmp_path):
    config = fixture(tmp_path)
    config["captures"][1].update(
        path=config["captures"][0]["path"], sha256=config["captures"][0]["sha256"]
    )
    with pytest.raises(ValueError, match="reuse"):
        run(tmp_path, config)


def test_annotation_hash_tampering_is_rejected(tmp_path):
    config = fixture(tmp_path)
    path = tmp_path / config["captures"][0]["path"]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA"):
        run(tmp_path, config)


def test_covariance_matches_independent_normal_matrix(tmp_path):
    result = run(tmp_path, fixture(tmp_path))
    for fit in result["facets"].values():
        rows = [p for p in fit["rows"] if p["partition"] == "fit"]
        x = np.array([[1.0, p["u"], p["u"] ** 3, -p["target_dx_dy"]] for p in rows])
        expected = np.linalg.inv(x.T @ x / 1e-12)
        np.testing.assert_allclose(fit["conditional_covariance"], expected, rtol=1e-12, atol=1e-25)


def test_outside_fit_support_is_not_extrapolated(tmp_path):
    config = fixture(tmp_path)
    edit(tmp_path, config, 1, lambda d: d["landmarks"][0].update(sample_column=0.0))
    result = run(tmp_path, config)
    assert result["validation"]["status"] == "unidentifiable"
    row = next(r for r in result["validation"]["rows"] if r["name"] == "l0-0")
    assert row["invalid_reason"] == "outside_fit_support" and row["prediction_m"] is None


def test_validation_grid_cannot_validate_identified_feed_offset(tmp_path):
    config = fixture(tmp_path)
    edit(
        tmp_path,
        config,
        1,
        lambda d: d.update(landmarks=[p for p in d["landmarks"] if p["target_dx_dy"] == 0]),
    )
    result = run(tmp_path, config)
    assert result["status"] == "identified"
    assert result["validation"]["status"] == "incomplete_coverage"
    assert all(
        c["conditional_design_rank"] == 3 and c["fit_rank"] == 4
        for c in result["validation"]["conditional_direction_coverage"].values()
    )


def test_slanted_only_identifies_offset_combination(tmp_path):
    config = fixture(tmp_path)
    for index in range(2):
        edit(
            tmp_path,
            config,
            index,
            lambda d: d.update(landmarks=[p for p in d["landmarks"] if p["target_dx_dy"] != 0]),
        )
    result = run(tmp_path, config)
    assert result["status"] == "partially_identifiable"
    for facet in result["facets"].values():
        assert facet["parameters"]["x_offset_m"]["value"] is None
        assert facet["parameters"]["y_offset_m"]["value"] is None
        assert facet["parameters"]["x_linear_m"]["value"] is not None
        assert facet["rank"] == 3


@pytest.mark.parametrize("failure", ["settings", "source_kind", "oracle_phase", "lost_sample"])
def test_inconsistent_observations_are_rejected(tmp_path, failure):
    config = fixture(tmp_path)

    def mutate(doc):
        if failure == "settings":
            cap = tmp_path / doc["capture_path"]
            data = json.loads(cap.read_bytes())
            data["binding"]["simulation_hash"] = "different-instrument"
            cap.write_text(json.dumps(data), encoding="utf-8")
            doc["capture_sha256"] = sha256_file(cap)
        elif failure == "source_kind":
            doc["source_kind"] = "declared_measurement"
        elif failure == "oracle_phase":
            doc["timing"]["lines"][0]["anchor"]["visibility"] = "ORACLE_ONLY"
        else:
            doc["landmarks"][0]["line"] = 8

    edit(tmp_path, config, 1, mutate)
    with pytest.raises(ValueError):
        run(tmp_path, config)


def test_nonmonotonic_mapping_is_invalid_even_with_exact_residual(tmp_path):
    config = fixture(tmp_path)

    def reverse(doc):
        for p in doc["landmarks"]:
            p["target_x_m"] *= -1

    for index in range(2):
        edit(tmp_path, config, index, reverse)
    result = run(tmp_path, config)
    assert result["status"] == "invalid_geometry"
    assert all(f["mapping_status"] == "nonmonotonic" for f in result["facets"].values())


def test_cli_writes_report_and_does_not_overwrite(tmp_path):
    from zlscan.cli import main

    config = fixture(tmp_path)
    path = tmp_path / "fit.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    output = tmp_path / "result"
    args = ["ident", "--geometry-fit", str(path), "--out", str(output)]
    assert main(args) == 0
    result = json.loads((output / "run.json").read_bytes())
    assert result["config_sha256"] == sha256_file(path)
    assert (output / "geometry_residuals.svg").stat().st_size > 1000
    assert (output / "geometry_residuals.png").stat().st_size > 1000
    before = (output / "run.json").read_bytes()
    assert main(args) != 0
    assert (output / "run.json").read_bytes() == before


def test_same_row_new_epoch_cannot_borrow_phase(tmp_path):
    from zlscan.io.rawstream import FLAG_EPOCH_START, read_all

    config = fixture(tmp_path)

    def split(doc):
        original = read_all(tmp_path / doc["raw_path"])
        path = tmp_path / "split.zlraw"
        with RawWriter(path, original.session) as writer:
            for line in range(4):
                parts = [(0, 50, 0), (50, 50, 1)] if line == 0 else [(0, 100, 1)]
                for offset, size, epoch in parts:
                    writer.write_block(
                        epoch=epoch,
                        channel_mask=1,
                        clock_domain=1,
                        first_sample_index=line * 200 + offset,
                        samples=np.full((size, 1), 30000, np.uint16),
                        line_number=line,
                        flags=(FLAG_LINE_START if offset == 0 else 0)
                        | (FLAG_LINE_END if offset + size == 100 else 0)
                        | (FLAG_EPOCH_START if line == 0 and offset == 50 else 0),
                    )
        digest = sha256_file(path)
        doc.update(raw_path=path.name, raw_sha256=digest)
        doc["timing"]["raw_sha256"] = digest
        for row in doc["timing"]["lines"][1:]:
            row["sync"]["epoch"] = 1
        row = doc["timing"]["lines"][1]
        row["anchor"] = dict(
            row["sync"], kind="facet_index", capability_ref="synthetic/index", payload={"phase": 1}
        )
        cap_path = tmp_path / doc["capture_path"]
        cap = json.loads(cap_path.read_bytes())
        cap["raw_sha256"] = digest
        cap["binding"]["epoch_range"] = [0, 1]
        cap_path.write_text(json.dumps(cap), encoding="utf-8")
        doc["capture_sha256"] = sha256_file(cap_path)

    edit(tmp_path, config, 0, split)
    result = run(tmp_path, config)
    excluded = result["excluded_unresolved_landmarks"]
    assert excluded
    assert all(p["capture"] == "scan-0" and p["line"] == 0 for p in excluded)
    assert any(p["sample_column"] > 50 for p in excluded)


def test_missing_window_length_is_controlled_rejection(tmp_path):
    from zlscan.io.rawstream import read_all

    config = fixture(tmp_path)

    def remove_window(doc):
        original = read_all(tmp_path / doc["raw_path"])
        session = original.session.model_copy(deep=True)
        session.windows.pop("window_length")
        path = tmp_path / "missing-window.zlraw"
        with RawWriter(path, session) as writer:
            for block, segment in zip(original.blocks, original.segments, strict=True):
                writer.write_block(
                    epoch=block.epoch,
                    channel_mask=1,
                    clock_domain=block.clock_domain,
                    first_sample_index=block.first_sample_index,
                    samples=segment.samples,
                    flags=block.flags,
                    line_number=block.line_number,
                )
        digest = sha256_file(path)
        doc.update(raw_path=path.name, raw_sha256=digest)
        doc["timing"]["raw_sha256"] = digest
        cap_path = tmp_path / doc["capture_path"]
        cap = json.loads(cap_path.read_bytes())
        cap["raw_sha256"] = digest
        cap_path.write_text(json.dumps(cap), encoding="utf-8")
        doc["capture_sha256"] = sha256_file(cap_path)

    edit(tmp_path, config, 0, remove_window)
    with pytest.raises(ValueError, match="window length"):
        run(tmp_path, config)
