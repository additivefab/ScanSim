import numpy as np
import pytest

from zlscan.calib.delay import fit_step_response, fit_synchronous_response
from zlscan.contracts.events import Event


@pytest.mark.parametrize("amplitude", [-0.7, 0.8])
def test_separates_fixed_delay_and_first_order_response_for_both_polarities(amplitude):
    edge_tick = 1_000_000_000
    ticks = edge_tick + np.arange(-6000, 8000, 100, dtype=np.int64)
    delay, tau = 350, 500
    values = 1.2 + amplitude * -np.expm1(-np.maximum(ticks - edge_tick - delay, 0) / tau)
    result = fit_step_response(ticks, values, edge_tick, reference="synchronous_injection")
    assert result.delay_ticks == delay
    assert result.registration_ticks is None
    assert result.response_tau_ticks == pytest.approx(tau, abs=0.01)

    registration = fit_step_response(ticks, values, edge_tick, reference="nominal_registration")
    assert registration.delay_ticks is None
    assert registration.registration_ticks == delay


def test_constant_trace_is_unidentifiable():
    ticks = np.arange(100, dtype=np.int64)
    with pytest.raises(ValueError, match="contrast"):
        fit_step_response(ticks, np.ones(100), 30, reference="synchronous_injection")


def test_injection_reference_rejects_oracle_and_wrong_epoch():
    monitor = Event(
        kind="injection_monitor",
        origin="SIMULATED",
        visibility="OBSERVABLE",
        derivation="SENSOR",
        clock_domain=1,
        count=10,
        tick=1000,
        session_id=3,
        epoch=1,
        capability_ref="fixture/injection_monitor",
    )
    ticks = np.arange(0, 5000, 100, dtype=np.int64)
    values = -np.expm1(-np.maximum(ticks - 1300, 0) / 400)
    assert (
        fit_synchronous_response(ticks, values, monitor, session_id=3, epoch=1).delay_ticks == 300
    )
    with pytest.raises(ValueError, match="observable injection monitor"):
        fit_synchronous_response(ticks, values, monitor, session_id=3, epoch=2)
    oracle = Event.model_validate({**monitor.model_dump(), "visibility": "ORACLE_ONLY"})
    with pytest.raises(ValueError, match="observable injection monitor"):
        fit_synchronous_response(ticks, values, oracle, session_id=3, epoch=1)
