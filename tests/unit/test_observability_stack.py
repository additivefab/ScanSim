"""Signal registry, trace summaries, replay and causality on the forward chain (S3-T12..T15)."""

import numpy as np
import pytest

from zlscan.adc.patterns import make_source, ramp
from zlscan.adc_link.link import LinkConfig, transmit
from zlscan.causality.lineage import dependency_steps, sample_lineage
from zlscan.config.presets import demo_config
from zlscan.contracts.events import Visibility
from zlscan.contracts.query import QueryStatus
from zlscan.contracts.time import ClockDomain
from zlscan.engine.chain import Chain
from zlscan.replay.replay import replay_window, run_with_checkpoints
from zlscan.telemetry.registry import PROBE_TO_SIGNAL, chain_signals
from zlscan.trace.recorder import record
from zlscan.truth import afe

STEPS = 10_000


def quiet(cfg):
    cfg.detector = type(cfg.detector)(**{**cfg.detector.__dict__, "shot_noise": False})
    return cfg


# ---- telemetry ------------------------------------------------------------------------------


def test_only_the_adc_code_is_observable():
    sigs = {s.signal_id: s for s in chain_signals(demo_config())}
    assert sigs["adc.code"].visibility is Visibility.OBSERVABLE
    assert all(s.visibility is Visibility.ORACLE_ONLY for k, s in sigs.items() if k != "adc.code")
    assert set(PROBE_TO_SIGNAL.values()) <= set(sigs)


def test_probe_selection_and_recording_policy_do_not_change_codes():
    base = Chain(demo_config()).run_steps(STEPS, 1500).codes
    for probes in (set(), {"power_w"}, {"power_w", "current_a", "v_adc", "t_eff", "x_m", "line"}):
        r = record(demo_config(), STEPS, probes, chunk=777)
        assert np.array_equal(r["codes"], base)


# ---- trace ----------------------------------------------------------------------------------


def test_a_one_step_pulse_survives_a_coarse_summary():
    cfg = quiet(demo_config())
    cfg.injections = [(4321, 1, 5e-3)]
    r = record(cfg, STEPS, {"power_w"}, base=256, keep_raw=True)
    raw = r["raw"]["power_w"]
    assert raw.argmax() == 4321
    pyr = r["pyramids"]["power_w"]
    kind, summ = pyr.query(0, STEPS, pixel_width=8, raw=raw)
    assert kind == "summary"
    assert summ[:, 1].max() == pytest.approx(raw.max(), rel=1e-6)  # the peak is still there
    mean_based = raw[: len(raw) // 256 * 256].reshape(-1, 256).mean(axis=1).max()
    assert mean_based < 0.5 * raw.max(), "averaging would have hidden it"


def test_events_are_indexed_for_flags():
    cfg = quiet(demo_config())
    cfg.detector = type(cfg.detector)(**{**cfg.detector.__dict__, "anode_limit_a": 1e-12})
    r = record(cfg, STEPS, set())
    assert r["events"]["pmt_over_limit"], "a tiny limit must flag samples"


# ---- replay ---------------------------------------------------------------------------------


def test_replay_from_the_nearest_checkpoint_matches_the_original_run():
    cfg = demo_config()
    full, store = run_with_checkpoints(cfg, 4 * STEPS, interval_steps=STEPS, chunk=2500)
    res = replay_window(cfg, store, start_step=2 * STEPS + 1234, n_steps=3000)
    assert res.status is QueryStatus.REPLAYED and res.checkpoint_step == 2 * STEPS
    lo = -(-(2 * STEPS + 1234) // cfg.os)
    hi = (2 * STEPS + 1234 + 3000 - 1) // cfg.os
    assert np.array_equal(res.out.codes, full.codes[lo : hi + 1])


def test_replay_with_probes_does_not_alter_the_run():
    cfg = demo_config()
    full, store = run_with_checkpoints(cfg, 2 * STEPS, interval_steps=STEPS)
    a = replay_window(cfg, store, STEPS + 10, 500)
    b = replay_window(cfg, store, STEPS + 10, 500, probes={"v_adc", "power_w"})
    assert np.array_equal(a.out.codes, b.out.codes) and set(b.out.probes) == {"v_adc", "power_w"}


def test_checkpoint_blobs_are_small_and_counted():
    _, store = run_with_checkpoints(demo_config(), 3 * STEPS, interval_steps=STEPS)
    assert len(store.blobs) == 4 and 0 < store.total_bytes < 20_000


def test_replay_needs_a_checkpoint_at_or_before_the_window():
    from zlscan.replay.replay import CheckpointStore

    with pytest.raises(LookupError):
        replay_window(demo_config(), CheckpointStore(10), 5, 5)


# ---- causality ------------------------------------------------------------------------------


def linear_chain():
    cfg = quiet(demo_config())
    cfg.afe = afe.AfeConfig(kind="linear", polarity=1, v0=0.2, r_t_ohm=2e4, pole_hz=3e6,
                            aa_poles_hz=(2e6,))  # fmt: skip
    return cfg


def test_an_injected_pulse_only_changes_samples_inside_its_dependency_window():
    base_cfg = linear_chain()
    pulse_cfg = linear_chain()
    pulse_cfg.injections = [(3000, 40, 2e-6)]
    a = Chain(base_cfg).run_steps(STEPS, 2000).codes
    b = Chain(pulse_cfg).run_steps(STEPS, 2000).codes
    dt = 1 / (base_cfg.fs_hz * base_cfg.os)
    window = dependency_steps([3e6, 2e6], dt, eps=1e-6 / 65536)
    first_changed = 3000 // base_cfg.os
    last_allowed = (3000 + 40 + window) // base_cfg.os + 1
    changed = np.flatnonzero(a != b)
    assert changed.size and changed.min() >= first_changed - 1
    assert changed.max() <= last_allowed


def test_samples_before_the_pulse_are_untouched():
    cfg = linear_chain()
    cfg.injections = [(6000, 10, 2e-6)]
    a = Chain(linear_chain()).run_steps(STEPS, 3000).codes
    b = Chain(cfg).run_steps(STEPS, 3000).codes
    assert np.array_equal(a[: 6000 // 4 - 1], b[: 6000 // 4 - 1])


def test_dependency_window_grows_with_a_slower_pole_and_a_tighter_eps():
    assert dependency_steps([1e6], 1e-8) < dependency_steps([1e5], 1e-8)
    assert dependency_steps([1e6], 1e-8, 1e-9) > dependency_steps([1e6], 1e-8, 1e-3)
    with pytest.raises(ValueError):
        dependency_steps([], 1e-8)


def test_sample_lineage_reports_the_four_times_and_the_path():
    src = make_source({0: ramp(0, 1, 20)}, ClockDomain(1, 50_000_000))
    out = transmit(src, LinkConfig(latency_half_cycles=24, propagation_ps=1500))
    lin = sample_lineage(out, 5, ["rx", "net"], block=2)
    assert (
        lin["sample_time"] < lin["adc_output_time"] < lin["pin_arrival_time"] <= lin["capture_time"]
    )
    assert lin["queue_path"] == ["rx", "net"] and lin["sample_index"] == 5
