"""Control state machine and epochs."""

import pytest

from zlscan.adc import patterns
from zlscan.contracts.time import ClockDomain
from zlscan.control.state import Controller, State, TransitionError, split_by_epoch

DOM = ClockDomain(id=1, freq_num=50_000_000)


def to_scanning(c):
    for st in (State.CONFIGURED, State.SETTLING, State.READY, State.SCANNING):
        c.to(st, 0)


def test_the_normal_path_is_allowed_and_a_shortcut_is_not():
    c = Controller()
    to_scanning(c)
    c.to(State.DRAINING, 1)
    c.to(State.DONE, 2)
    with pytest.raises(TransitionError):
        Controller().to(State.SCANNING, 0)


def test_samples_taken_before_the_change_keep_the_old_epoch_even_if_still_in_flight():
    c = Controller()
    to_scanning(c)
    change = DOM.tick_of(100)
    c.request("gain", 2, request_tick=change - 5_000, effective_tick=change)
    src = patterns.make_source({0: patterns.ramp(0, 1, 200)}, DOM)
    parts = split_by_epoch(src, c)
    assert [p.epoch for p in parts] == [0, 1]
    assert parts[0].first_index == 0 and len(parts[0]) == 100
    assert parts[1].first_index == 100 and len(parts[1]) == 100


def test_request_effective_and_readback_are_three_separate_records():
    c = Controller()
    c.request("clock_mode", "dco_x2", request_tick=10, effective_tick=50, readback_tick=90,
              readback_value="dco_x2")  # fmt: skip
    kinds = [(x.kind, x.tick) for x in c.log]
    assert kinds == [("request", 10), ("effective", 50), ("readback", 90)]
    assert c.readback_confirmed("clock_mode")


def test_a_wrong_readback_is_not_a_confirmation():
    c = Controller()
    c.request("coding", "twos", 0, 10, readback_tick=20, readback_value="offset")
    assert not c.readback_confirmed("coding")
    c2 = Controller()
    c2.request("coding", "twos", 0, 10)
    assert not c2.readback_confirmed("coding")


def test_a_command_cannot_take_effect_before_it_is_requested():
    with pytest.raises(ValueError):
        Controller().request("x", 1, request_tick=10, effective_tick=5)


@pytest.mark.parametrize("state", [State.READY, State.SCANNING])
def test_a_reset_starts_a_new_epoch_and_marks_an_interrupted_scan_incomplete(state):
    c = Controller()
    to_scanning(c)
    if state is State.READY:
        c = Controller()
        for st in (State.CONFIGURED, State.SETTLING, State.READY):
            c.to(st, 0)
    before = c.epochs[-1]
    c.reset(1000)
    assert c.epochs[-1].id == before.id + 1 and c.epochs[-1].reset
    assert before.incomplete == (state is State.SCANNING)
    assert c.state is State.UNCONFIGURED


def test_a_fault_or_abort_from_any_state_marks_the_epoch_incomplete():
    c = Controller()
    to_scanning(c)
    c.to(State.ABORTED, 5)
    assert c.epochs[-1].incomplete
