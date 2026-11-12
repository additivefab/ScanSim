"""Analytic responses verify an explicit candidate observer, not an actual scope."""

import json
import math

import numpy as np
import pytest

from zlscan.cli import main
from zlscan.observation.scope import ScopeObserverConfig, observe_voltage


def config(**changes):
    d = dict(
        probe_ratio=10.0,
        probe_compensated=True,
        probe_bandwidth_hz=0.0,
        scope_bandwidth_hz=0.0,
        bandwidth_limit_hz=0.0,
        coupling="DC",
        aperture_s=0.0,
        initial_state="steady",
        time_offset_s=0.0,
        parameter_source="synthetic assumed observer",
    )
    d.update(changes)
    return ScopeObserverConfig(**d)


def values(result):
    return np.asarray(result["voltage_v"], dtype=float)


def test_ideal_observer_interpolates_and_applies_probe_compensation_once():
    t = np.array([0.0, 1.0, 2.0])
    q = np.array([0.5, 1.5])
    np.testing.assert_allclose(values(observe_voltage(t, 2 * t, q, config())), [1, 3], atol=1e-14)
    np.testing.assert_allclose(
        values(observe_voltage(t, 2 * t, q, config(probe_compensated=False))),
        [0.1, 0.3],
        atol=1e-14,
    )


def test_one_pole_step_has_analytic_response_and_explicit_initial_state():
    tau = 1e-3
    t = np.array([0.0, 10 * tau])
    q = np.linspace(0, 10 * tau, 31)
    fc = 1 / (2 * math.pi * tau)
    r = observe_voltage(t, np.ones(2), q, config(scope_bandwidth_hz=fc, initial_state="zero"))
    # Absolute voltage tolerance for numerical matrix exponential vs analytic response.
    np.testing.assert_allclose(values(r), 1 - np.exp(-q / tau), rtol=0, atol=2e-13)
    r = observe_voltage(t, np.ones(2), q, config(scope_bandwidth_hz=fc))
    np.testing.assert_allclose(values(r), 1, rtol=0, atol=2e-13)


def test_repeated_poles_are_composed_without_intermediate_linearization():
    tau = 0.01
    q = np.linspace(0, 8 * tau, 29)
    fc = 1 / (2 * math.pi * tau)
    r = observe_voltage(
        np.array([0.0, 8 * tau]),
        np.ones(2),
        q,
        config(scope_bandwidth_hz=fc, probe_bandwidth_hz=fc, initial_state="zero"),
    )
    np.testing.assert_allclose(values(r), 1 - (1 + q / tau) * np.exp(-q / tau), rtol=0, atol=3e-13)


def test_ac_coupling_and_aperture_have_known_responses():
    tau = 0.01
    fc = 1 / (2 * math.pi * tau)
    q = np.array([0.01, 0.03, 0.06])
    r = observe_voltage(
        np.array([0.0, 0.1]),
        np.ones(2),
        q,
        config(coupling="AC", ac_corner_hz=fc, initial_state="zero"),
    )
    np.testing.assert_allclose(values(r), np.exp(-q / tau), rtol=0, atol=2e-13)
    aperture = 0.004
    r = observe_voltage(
        np.array([0.0, 0.1]),
        np.ones(2),
        q,
        config(scope_bandwidth_hz=fc, initial_state="zero", aperture_s=aperture),
    )
    expected = 1 - tau / aperture * (
        np.exp(-(q - aperture / 2) / tau) - np.exp(-(q + aperture / 2) / tau)
    )
    np.testing.assert_allclose(values(r), expected, rtol=0, atol=2e-13)


def test_aperture_crossing_source_knots_and_outside_support_are_explicit():
    r = observe_voltage(
        np.array([0.0, 1.0, 2.0]),
        np.array([0.0, 1.0, 0.0]),
        np.array([0.0, 1.0, 2.0]),
        config(aperture_s=1.0),
    )
    assert r["voltage_v"] == [None, 0.75, None]
    assert r["invalid_reason"] == ["outside_model_support", None, "outside_model_support"]
    assert r["alignment_uncertainty_s"] is None
    assert r["origin"] == "MODEL_ESTIMATE"
    assert not r["hard_acceptance_eligible"]


def test_offset_is_explicit_not_fitted_and_state_is_invariant_to_extra_source_knots():
    coarse = observe_voltage(
        np.array([0.0, 2.0]),
        np.array([0.0, 2.0]),
        np.array([0.0, 0.5]),
        config(time_offset_s=0.5, scope_bandwidth_hz=0.5),
    )
    fine = observe_voltage(
        np.linspace(0, 2, 41),
        np.linspace(0, 2, 41),
        np.array([0.0, 0.5]),
        config(time_offset_s=0.5, scope_bandwidth_hz=0.5),
    )
    np.testing.assert_allclose(values(coarse), values(fine), rtol=0, atol=2e-13)


def test_unknown_response_is_not_implicitly_ideal():
    with pytest.raises(ValueError, match="unknown"):
        observe_voltage(np.array([0.0, 1.0]), np.ones(2), np.array([0.5]), ScopeObserverConfig())
    with pytest.raises(ValueError):
        observe_voltage(np.array([0.0, 1.0]), np.ones(2), np.array([0.5]), config(coupling="AC"))


def test_observer_cli_attaches_nominal_prediction_without_mutating_observations(tmp_path):
    source = tmp_path / "scope.csv"
    source.write_text("t,CH1\n0,1\n1,2\n2,3\n")
    meta = tmp_path / "metadata.json"
    meta.write_text(
        json.dumps(
            dict(
                time_column="t",
                time_unit="s",
                channels=[
                    dict(
                        column="CH1",
                        unit="V",
                        probe_ratio=10.0,
                        probe_compensated=True,
                        coupling="DC",
                        bandwidth_limit_hz=0.0,
                    )
                ],
            )
        )
    )
    model = tmp_path / "model.csv"
    model.write_text("time_s,voltage_v\n0,1\n2,3\n")
    observer = tmp_path / "observer.json"
    observer.write_text(config().model_dump_json())
    before = [p.read_bytes() for p in [source, meta, model, observer]]
    args = [
        "ident",
        "--csv",
        str(source),
        "--metadata",
        str(meta),
        "--out",
        str(tmp_path / "out"),
        "--model-waveform",
        str(model),
        "--observer",
        str(observer),
        "--observe-channel",
        "CH1",
    ]
    assert main(args) == 0
    r = json.loads((tmp_path / "out/run.json").read_text())
    assert r["observation_model"]["nominal_residual_v"] == [0.0, 0.0, 0.0]
    assert r["observation_model"]["alignment_status"] == "declared_offset_only"
    assert r["channels"][0]["values_si"] == [1.0, 2.0, 3.0]
    assert before == [p.read_bytes() for p in [source, meta, model, observer]]
    assert not r["hard_acceptance_eligible"]


def test_probe_metadata_conflict_is_rejected_before_output(tmp_path):
    from zlscan.ident.observe import attach_scope_observer

    capture = dict(
        time_s=[0.0, 1.0],
        channels=[dict(column="CH1", unit_si="V", values_si=[1.0, 1.0])],
        metadata=dict(channels=[dict(column="CH1", probe_ratio=1.0)]),
    )
    with pytest.raises(ValueError, match="probe_ratio"):
        attach_scope_observer(capture, np.array([0.0, 1.0]), np.ones(2), config(), "CH1")


def test_sinusoid_matches_single_pole_response_with_declared_linear_source_error():
    t = np.linspace(0, 2, 401)
    q = np.linspace(0, 2, 101)
    w, k = 2 * math.pi, math.pi
    expected = k / (k * k + w * w) * (k * np.sin(w * q) - w * np.cos(w * q) + w * np.exp(-k * q))
    r = observe_voltage(t, np.sin(w * t), q, config(scope_bandwidth_hz=0.5, initial_state="zero"))
    # 0.005 s source intervals approximate a sine by straight segments; this voltage
    # tolerance includes that explicit source approximation, not a hardware limit.
    np.testing.assert_allclose(values(r), expected, rtol=0, atol=5e-5)


def test_small_aperture_at_large_origin_preserves_dc_without_prefix_cancellation():
    t = np.array([1e12, 1e12 + 2])
    r = observe_voltage(t, np.array([2.0, 2.0]), np.array([1e12 + 1]), config(aperture_s=0.01))
    np.testing.assert_allclose(values(r), [2.0], rtol=0, atol=1e-13)
    t = np.array([1e16, 1e16 + 10])
    r = observe_voltage(t, np.ones(2), np.array([1e16 + 4]), config(aperture_s=0.1))
    assert r["voltage_v"] == [None]
    assert r["invalid_reason"] == ["aperture_below_time_resolution"]


def test_observer_cli_requires_all_attachment_arguments():
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "ident",
                "--csv",
                "scope.csv",
                "--metadata",
                "scope.json",
                "--out",
                "out",
                "--model-waveform",
                "model.csv",
            ]
        )
    assert exc.value.code == 2
