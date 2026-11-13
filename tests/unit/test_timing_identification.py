"""Timestamp-domain fitting preserves shared endpoint covariance and observable phase identity."""

import hashlib
import json

import numpy as np
import pytest

from zlscan.ident.timing import TimingFitConfig, identify_timing_files


def fixture(root, *, anchors=True, indices=None):
    periods = [1000000, 1100000, 900000]
    cases = []
    for k, partition in enumerate(("fit", "fit", "validation")):
        raw = root / f"raw-{k}.txt"
        raw.write_text(f"explicit synthetic acquisition {k}\n")
        edges = []
        ids = list(range(0, 61)) if indices is None else indices
        origin = 2**60 + k * 10**9
        for n in ids:
            tick = origin + sum(periods[j % 3] for j in range(n))
            event = dict(
                kind="line_sync",
                origin="SIMULATED",
                visibility="OBSERVABLE",
                derivation="SENSOR",
                clock_domain=7,
                count=n,
                tick=tick,
                session_id=k,
                epoch=0,
                capability_ref="synthetic/counter",
            )
            anchor = None
            if anchors and n == ids[0]:
                anchor = dict(
                    event,
                    kind="facet_index",
                    capability_ref="synthetic/index",
                    payload={"phase": n % 3},
                )
            edges.append(dict(line_index=str(n), sync=event, anchor=anchor))
        body = dict(
            format="zlscan.sync-observations/1",
            source_kind="synthetic",
            device="synthetic instrument",
            measurement_point="line sync",
            settings_source="explicit synthetic timestamp/counter protocol",
            extraction_method="explicit event export, not inferred phase",
            raw_source=raw.name,
            raw_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
            session_id=k,
            epoch=0,
            clock_domain=7,
            counter_capability_ref="synthetic/counter",
            phase_capability_ref="synthetic/index" if anchors else None,
            edges=edges,
        )
        p = root / f"edges-{k}.json"
        p.write_text(json.dumps(body))
        cases.append(
            dict(
                name=f"acq-{k}",
                path=p.name,
                sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
                partition=partition,
                standard_uncertainty_ps=20.0,
                uncertainty_source="independent Gaussian synthetic timestamp protocol",
            )
        )
    return dict(
        format="zlscan.timing-fit-config/1",
        facets=3,
        confidence_level=0.95,
        rank_rtol=1e-10,
        assumption_source="explicit synthetic counter and known timestamps",
        error_model="independent_gaussian_known_timestamp_sigma",
        validation_max_abs_residual_ps=1000.0,
        captures=cases,
    )


def run(root, c):
    return identify_timing_files(TimingFitConfig.model_validate_json(json.dumps(c)), root)


def test_exact_large_ticks_and_per_facet_periods(tmp_path):
    c = fixture(tmp_path)
    r = run(tmp_path, c)
    assert r["status"] == "identified"
    f = r["facet_period_fit"]
    assert f["rank"] == 5
    for name, expected in zip(
        ("period_0_ps", "period_1_ps", "period_2_ps"), (1e6, 1.1e6, 0.9e6), strict=True
    ):
        p = f["parameters"][name]
        assert p["value"] == pytest.approx(expected, abs=1e-5)
        assert p["conditional_interval"][0] < expected < p["conditional_interval"][1]
    assert r["validation"]["status"] == "compatible"
    assert not r["parameter_upgrades"] and not r["hard_acceptance_eligible"]
    assert r["captures"][0]["origin_tick_ps"] == str(2**60)
    assert len(f["correlation_ps"]) == 3


def test_missing_counts_do_not_compress_time_or_invent_edges(tmp_path):
    ids = [0, 1, 3, 4, 6, 8, 9, 10, 12, 15, 18, 19, 21, 24, 27, 30, 33, 36]
    r = run(tmp_path, fixture(tmp_path, indices=ids))
    assert r["status"] == "identified"
    assert r["captures"][0]["missing_line_counts"] == str(37 - len(ids))
    assert len(r["captures"][0]["line_indices"]) == len(ids)
    assert r["facet_period_fit"]["parameters"]["period_1_ps"]["value"] == pytest.approx(
        1.1e6, abs=1e-5
    )


def test_without_reference_phase_is_unidentifiable(tmp_path):
    r = run(tmp_path, fixture(tmp_path, anchors=False))
    assert r["status"] == "partially_identifiable"
    assert r["global_period_fit"]["parameters"]["period_ps"]["value"] is not None
    assert r["facet_period_fit"]["status"] == "unidentifiable"
    assert r["validation"]["status"] == "unidentifiable"


def test_cycle_only_data_identifies_total_not_each_facet(tmp_path):
    r = run(tmp_path, fixture(tmp_path, indices=list(range(0, 61, 3))))
    assert r["status"] == "partially_identifiable"
    f = r["facet_period_fit"]
    assert all(p["value"] is None for p in f["parameters"].values())
    assert f["cycle_period"]["value"] == pytest.approx(3000000.0, abs=1e-5)
    assert r["validation"]["status"] == "compatible"


def test_holdout_never_changes_fitted_parameters(tmp_path):
    c = fixture(tmp_path)
    a = run(tmp_path, c)
    p = tmp_path / c["captures"][-1]["path"]
    d = json.loads(p.read_text())
    d["edges"][-1]["sync"]["tick"] += 10000
    p.write_text(json.dumps(d))
    c["captures"][-1]["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    b = run(tmp_path, c)
    assert a["facet_period_fit"] == b["facet_period_fit"]
    assert b["validation"]["status"] == "mismatch"


@pytest.mark.parametrize(
    "kind",
    [
        "oracle",
        "inferred",
        "identity",
        "duplicate",
        "counter",
        "anchor",
        "digest",
        "raw",
        "reuse",
        "span",
    ],
)
def test_bad_sources_and_ambiguous_edges_are_rejected(tmp_path, kind):
    c = fixture(tmp_path)
    case = c["captures"][0]
    p = tmp_path / case["path"]
    d = json.loads(p.read_text())
    if kind == "oracle":
        d["edges"][0]["sync"]["visibility"] = "ORACLE_ONLY"
    elif kind == "inferred":
        d["edges"][0]["sync"]["derivation"] = "INFERRED"
    elif kind == "identity":
        d["edges"][1]["sync"]["epoch"] = 1
    elif kind == "duplicate":
        d["edges"][1]["sync"]["tick"] = d["edges"][0]["sync"]["tick"]
    elif kind == "counter":
        d["edges"][1]["line_index"] = "0"
    elif kind == "anchor":
        d["edges"][3]["anchor"] = dict(
            d["edges"][3]["sync"],
            kind="facet_index",
            capability_ref="synthetic/index",
            payload={"phase": 1},
        )
    elif kind == "digest":
        case["sha256"] = "0" * 64
    elif kind == "raw":
        (tmp_path / d["raw_source"]).write_text("changed source")
    elif kind == "reuse":
        c["captures"][-1].update(path=case["path"], sha256=case["sha256"])
    elif kind == "span":
        d["edges"][-1]["sync"]["tick"] += 2**54
    if kind not in ("digest", "reuse"):
        p.write_text(json.dumps(d))
        case["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    r = run(tmp_path, c)
    assert r["status"] == "rejected" and not r["parameter_upgrades"]


def test_timestamp_covariance_is_not_independent_period_difference_approximation(tmp_path):
    c = fixture(tmp_path, anchors=False)
    c["facets"] = 1
    r = run(tmp_path, c)
    # Two captures, 61 independent timestamp errors each, a nuisance intercept per capture.
    x = np.arange(61, dtype=float)
    expected = 20.0**2 / (2 * np.sum((x - x.mean()) ** 2))
    actual = r["global_period_fit"]["parameters"]["period_ps"]["standard_uncertainty"] ** 2
    assert actual == pytest.approx(expected, rel=1e-9)


def test_large_absolute_counters_are_differenced_as_integers(tmp_path):
    c = fixture(tmp_path)
    for case in c["captures"]:
        path = tmp_path / case["path"]
        d = json.loads(path.read_text())
        for e in d["edges"]:
            e["line_index"] = str(int(e["line_index"]) + 2**60)
            e["sync"]["count"] += 2**60
        path.write_text(json.dumps(d))
        case["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    r = run(tmp_path, c)
    assert r["status"] == "identified" and r["captures"][0]["line_indices"][1] == str(2**60 + 1)
    assert r["facet_period_fit"]["parameters"]["period_1_ps"]["value"] == pytest.approx(
        1.1e6, abs=1e-5
    )


def test_unscored_validation_anchor_and_no_threshold(tmp_path):
    c = fixture(tmp_path)
    c["validation_max_abs_residual_ps"] = None
    r = run(tmp_path, c)
    assert r["validation"]["status"] == "not_assessed"
    v = r["validation"]["rows"][0]
    assert len(v["points"]) == 60 and v["points"][0]["counter_delta"] == "1"
    c["captures"] = c["captures"][:2]
    assert run(tmp_path, c)["validation"]["status"] == "not_executed"


def test_interval_gaps_are_span_averages_not_fabricated_instantaneous_rates(tmp_path):
    c = fixture(tmp_path, indices=[0, 1, 2, 5, 8, 9, 11, 12, 14, 15, 17, 18])
    r = run(tmp_path, c)
    v = r["captures"][0]["intervals"][2]
    assert v["counter_delta"] == "3" and v["elapsed_ticks_ps"] == "3000000"
    assert not v["is_single_line_interval"] and v["missing_counts"] == "2"
    assert v["span_average_line_rate_hz"] == pytest.approx(1e6)
    assert v["starting_phase"] == 2
    assert v["conditional_period_standard_uncertainty_ps"] == pytest.approx(np.sqrt(2) * 20 / 3)


def test_cli_artifacts_and_unidentifiable_exit(tmp_path):
    from zlscan.cli import main

    c = fixture(tmp_path)
    p = tmp_path / "fit.json"
    p.write_text(json.dumps(c))
    assert main(["ident", "--timing-fit", str(p), "--out", str(tmp_path / "result")]) == 0
    assert (
        json.loads((tmp_path / "result/run.json").read_text())["validation"]["status"]
        == "compatible"
    )
    assert (tmp_path / "result/timing_residuals.png").read_bytes().startswith(b"\x89PNG")
    c = fixture(tmp_path, anchors=False)
    p.write_text(json.dumps(c))
    assert main(["ident", "--timing-fit", str(p), "--out", str(tmp_path / "unknown")]) == 1
    assert (tmp_path / "unknown/timing_residuals.svg").stat().st_size > 1000
    with pytest.raises(SystemExit):
        main(["ident", "--timing-fit", str(p), "--metadata", "x", "--out", "x"])
