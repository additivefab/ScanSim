"""Scan geometry is one set of coupled constraints; inputs may not be chosen independently."""

import math

import pytest

from zlscan.budget.geometry import VARIABLES, solve
from zlscan.contracts.param import Evidence

TWO_PI = 2 * math.pi


def test_package_b_example_gives_8_378_mpixel_per_second_not_10():
    """Docs: 6 facets, 10000 rpm, 200 mm focal length, 50 um pixels -> ~8.378 Mpx/s, 418.88 m/s."""
    r = solve(dict(facets=6, rpm=10_000, focal_m=0.2, pitch_x_m=50e-6))
    assert r.consistent
    assert r.values["f_line_hz"] == pytest.approx(1000.0)
    assert r.values["v_x_m_s"] == pytest.approx(418.879, rel=1e-5)
    assert r.values["pixel_rate_hz"] == pytest.approx(8.37758e6, rel=1e-5)


def test_package_b_example_needs_a_238_73_mm_focal_length_for_10_mpixel_per_second():
    r = solve(dict(facets=6, rpm=10_000, pitch_x_m=50e-6, pixel_rate_hz=10e6))
    assert r.values["focal_m"] == pytest.approx(0.238732, rel=1e-5)


def test_package_a_budget_scenario_is_fully_consistent():
    given = dict(facets=6, rpm=10_000, duty=0.7, width_m=0.35, height_m=0.43,
                 pitch_x_m=50e-6, pitch_y_m=50e-6, fs_hz=20e6, spp=2)  # fmt: skip
    r = solve(given)
    assert r.consistent and r.conflicts == []
    v = r.values
    assert v["nx"] == pytest.approx(7000) and v["ny"] == pytest.approx(8600)
    assert v["line_period_s"] == pytest.approx(1e-3)
    assert v["active_time_s"] == pytest.approx(700e-6)
    assert v["v_x_m_s"] == pytest.approx(500.0)
    assert v["v_y_m_s"] == pytest.approx(0.05)
    assert v["focal_m"] == pytest.approx(0.238732, rel=1e-5)
    assert v["frame_time_s"] == pytest.approx(8.6)
    assert v["theta_max_rad"] == pytest.approx(0.7330, rel=1e-3)


def test_every_derived_value_is_marked_derived_and_names_its_inputs():
    r = solve(dict(facets=6, rpm=10_000, focal_m=0.2, pitch_x_m=50e-6))
    assert r.provenance["f_line_hz"].kind == "derived"
    assert set(r.provenance["f_line_hz"].inputs) == {"facets", "rpm"}
    assert r.provenance["facets"].kind == "given"


def test_a_conflicting_extra_input_names_the_quantity_and_both_values():
    r = solve(dict(facets=6, rpm=10_000, focal_m=0.2, pitch_x_m=50e-6, pixel_rate_hz=10e6))
    assert not r.consistent
    (c,) = [c for c in r.conflicts if c.variable == "pixel_rate_hz"]
    assert c.given == 10e6 and c.derived == pytest.approx(8.37758e6, rel=1e-5)
    assert "pixel_rate_hz" in c.message and "focal_m" in c.message


def test_an_under_determined_problem_lists_what_is_still_unknown():
    r = solve(dict(facets=6))
    assert r.consistent and set(r.unresolved) == set(VARIABLES) - {"facets"}


def test_values_are_not_invented_when_inputs_are_missing():
    r = solve(dict(rpm=10_000, pitch_x_m=50e-6))
    assert "focal_m" not in r.values and "pixel_rate_hz" not in r.values


def test_the_weakest_evidence_of_the_inputs_carries_to_the_derived_value():
    given = dict(facets=6, rpm=10_000, focal_m=0.2, pitch_x_m=50e-6)
    evidence = {"facets": Evidence.MEASURED, "rpm": Evidence.MEASURED,
                "focal_m": Evidence.DATASHEET, "pitch_x_m": Evidence.ASSUMED}  # fmt: skip
    r = solve(given, evidence)
    assert r.weakest_evidence["f_line_hz"] is Evidence.MEASURED
    assert r.weakest_evidence["pixel_rate_hz"] is Evidence.ASSUMED
    assert r.weakest_evidence["v_x_m_s"] is Evidence.DATASHEET


def test_an_input_without_stated_evidence_counts_as_assumed():
    r = solve(dict(facets=6, rpm=10_000))
    assert r.weakest_evidence["f_line_hz"] is Evidence.ASSUMED


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf")])
def test_non_positive_or_non_finite_inputs_are_rejected(bad):
    with pytest.raises(ValueError, match="rpm"):
        solve(dict(rpm=bad))


def test_an_unknown_variable_name_is_rejected():
    with pytest.raises(ValueError, match="no_such"):
        solve(dict(no_such=1.0))


def test_the_relations_close_a_loop_and_agree_with_each_other():
    """facets, rpm, duty, spp, fs, pitch fix everything; width then follows two different ways."""
    r = solve(dict(facets=6, rpm=10_000, duty=0.7, pitch_x_m=50e-6, pitch_y_m=50e-6,
                   fs_hz=20e6, spp=2))  # fmt: skip
    assert r.consistent
    width_from_geometry = 2 * r.values["focal_m"] * r.values["theta_max_rad"]
    width_from_speed = r.values["v_x_m_s"] * r.values["active_time_s"]
    assert width_from_geometry == pytest.approx(width_from_speed, rel=1e-9)
    assert r.values["omega_rad_s"] == pytest.approx(TWO_PI * 10_000 / 60)
