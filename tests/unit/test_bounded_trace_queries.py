from types import SimpleNamespace

import numpy as np
import pytest

from zlscan.api.server import Service
from zlscan.contracts.time import ClockDomain
from zlscan.trace.pyramid import Pyramid
from zlscan.trace.window import raw_window, summary_window


def test_million_sample_adc_query_is_bounded_and_keeps_a_single_sample_spike():
    values = np.zeros(1_000_003, np.uint16)
    values[543_211] = 65535
    result = raw_window(values, 0, len(values), 31)
    assert result["status"] == "summary_only"
    assert len(result["values"]) <= 31
    assert max(row[1] for row in result["values"]) == 65535
    assert sum(row[4] for row in result["values"]) == len(values)
    assert result["values"][-1][3] == values[-1]
    fine = raw_window(values, 543_200, 543_220, 31)
    assert fine["level"] == "raw"
    assert fine["values"] == values[543_200:543_220].tolist()
    assert np.count_nonzero(values) == 1


def test_analogue_coarsening_is_bounded_and_reports_partial_boundary_support():
    values = np.zeros(100_003)
    values[66] = 10
    pyramid = Pyramid.build(values, base=64)
    result = summary_window(pyramid, 0, len(values), 1)
    assert len(result["values"]) == 1
    assert result["values"][0] == [0, 10, 0, 0, len(values)]
    boundary = summary_window(pyramid, 70, 400, 1)
    assert boundary["support_start"] == 64
    assert boundary["support_end"] == 448
    assert boundary["boundary_extra"]
    assert boundary["values"][0][1] == 10  # correctly belongs to the declared wider support
    assert summary_window(pyramid, 70, 80, 100)["status"] == "not_recorded"


@pytest.mark.parametrize("width", [0, -1, 4097, True])
def test_invalid_display_budgets_are_rejected(width):
    with pytest.raises(ValueError):
        raw_window(np.zeros(10, np.uint16), 0, 10, width)


def test_service_preserves_exact_large_time_and_rational_sampling_metadata():
    clock = ClockDomain(1, 125_000_003, phase_ticks=2**53 + 17)
    run = SimpleNamespace(
        cfg=SimpleNamespace(os=4, fs_hz=125_000_003, domain_id=1),
        steps=40,
        codes=np.arange(10, dtype=np.uint16),
        link=SimpleNamespace(domain=clock, first_index=123),
        pyramids={},
    )
    response = Service().traces(run, "adc.code", 4, 20, 100)
    time = response["sample_time"]
    assert time["first_sample_index"] == "124"
    assert time["first_tick_ps"] == str(clock.tick_of(124))
    assert time["last_tick_ps"] == str(clock.tick_of(127))
    assert time["period_ps"] == {"numerator": "1000000000000", "denominator": "125000003"}
    assert int(time["first_tick_ps"]) > 2**53
    assert response["first_step"] == 4 and response["block_steps"] == 4


def test_service_clips_negative_windows_and_does_not_wrap_array_slices():
    run = SimpleNamespace(
        cfg=SimpleNamespace(os=4, fs_hz=1_000_000, domain_id=1),
        steps=40,
        codes=np.arange(10, dtype=np.uint16),
        link=None,
        pyramids={},
    )
    service = Service()
    response = service.traces(run, "adc.code", -100, 8, 100)
    assert response["values"] == [0, 1]
    assert response["first_step"] == 0
    assert response["requested_window"] == [-100, 8]
    empty = service.traces(run, "adc.code", 44, 80, 100)
    assert empty["status"] == "not_computed" and not empty.get("values")
    with pytest.raises(ValueError):
        service.traces(run, "adc.code", 12, 8, 100)


def test_http_lineage_times_and_identity_are_exact_strings_above_js_safe_integer():
    from zlscan.adc.patterns import ConvertedSamples
    from zlscan.adc_link.link import LinkConfig, transmit

    values = np.arange(10, dtype=np.uint16)
    clock = ClockDomain(1, 1_000_000, phase_ticks=2**53 + 17)
    source = ConvertedSamples(values.reshape(-1, 1), (0,), 123, 0, clock, 16)
    link = transmit(source, LinkConfig(latency_half_cycles=24, propagation_ps=1500))
    run = SimpleNamespace(run_id="clock-run", codes=values, link=link, session_id=9, epoch=0)
    lineage = Service().lineage(run, 124)["lineage"]
    for key in ("sample_time", "adc_output_time", "pin_arrival_time", "capture_time"):
        assert isinstance(lineage[key], str)
        assert int(lineage[key]) == int(getattr(link, key)[1])
    assert lineage["sample_identity"]["sample_index"] == "124"
    assert lineage["sample_identity"]["session_id"] == 9
    assert lineage["host_arrival_time"] is None
