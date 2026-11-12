"""Control history survives checkpoints without false confirmations or partial mutations."""

import json
from copy import deepcopy

import numpy as np
import pytest

from zlscan.adc.patterns import ConvertedSamples
from zlscan.contracts.time import ClockDomain
from zlscan.control.state import Controller, State, split_by_epoch


def history():
    control = Controller()
    for state in (State.CONFIGURED, State.SETTLING, State.READY, State.SCANNING):
        control.to(state, 0)
    control.request(
        "gain",
        {"range": [1, 2]},
        2**53 + 1,
        2**53 + 11,
        readback_tick=2**53 + 21,
        readback_value={"range": [1, 2]},
    )
    return control


@pytest.mark.parametrize(
    "operation",
    [
        lambda c: c.request("gain", 3, 2**53 + 2, 2**53 + 5),
        lambda c: c.request("gain", 3, 2**53 + 22, 2**53 + 30, 2**53 + 29, 3),
        lambda c: c.reset(2**53 + 5),
        lambda c: c.request("gain", float("nan"), 2**53 + 22, 2**53 + 30),
        lambda c: c.to(State.DRAINING, 1.5),
        lambda c: c.reset(2**63),
    ],
)
def test_invalid_action_does_not_change_state_or_log(operation):
    control = history()
    before = deepcopy(control.__dict__)
    with pytest.raises(ValueError):
        operation(control)
    assert control.__dict__ == before


def test_confirmation_belongs_to_request_and_respects_query_time_and_reset():
    control = Controller()
    control.request("mode", "x", 1, 5, 9, "x")
    assert not control.readback_confirmed("mode", at_tick=8)
    assert control.readback_confirmed("mode", at_tick=9)
    control.request("mode", "x", 11, 15)
    assert control.readback_confirmed("mode", at_tick=10)
    assert not control.readback_confirmed("mode")
    control.reset(20)
    assert not control.readback_confirmed("mode")


def test_config_and_log_do_not_alias_input_or_each_other():
    value = {"modes": [1, 2]}
    readback = deepcopy(value)
    control = Controller()
    control.request("config", value, 1, 2, 3, readback)
    value["modes"].append(3)
    readback["modes"].clear()
    assert control.readback_confirmed("config")
    control.log[0].detail["value"]["modes"].append(99)
    assert control.epochs[1].config["config"] == {"modes": [1, 2]}
    assert control.log[1].detail["value"] == {"modes": [1, 2]}


def test_control_checkpoint_restores_future_command_ledger_and_continuation():
    original = history()
    blob = original.checkpoint()
    saved = json.loads(blob)
    assert saved["log_cursor"] == str(len(original.log))
    assert saved["epochs"][1]["effective_tick"] == str(2**53 + 11)
    assert all(isinstance(row["tick"], str) for row in saved["log"])
    restored = Controller.restore(blob)
    assert restored.checkpoint() == blob
    assert not restored.readback_confirmed("gain", at_tick=2**53 + 20)
    assert restored.readback_confirmed("gain", at_tick=2**53 + 21)
    for control in (original, restored):
        control.reset(2**53 + 25)
        control.to(State.CONFIGURED, 2**53 + 25)
        control.request("gain", 4, 2**53 + 26, 2**53 + 27)
    assert original.checkpoint() == restored.checkpoint()
    assert original.epochs[1].incomplete and original.epochs[2].reset
    assert not restored.readback_confirmed("gain")


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(state="done"),
        lambda d: d.update(log_cursor="0"),
        lambda d: d.update(next_command_id="99"),
        lambda d: d["epochs"][1].update(incomplete=True),
        lambda d: d["log"][5]["detail"].update(command_id="99"),
    ],
)
def test_rehashed_inconsistent_history_is_still_rejected(change):
    control = history()
    value = json.loads(control.checkpoint())
    change(value)
    value.pop("checksum")
    value["checksum"] = Controller._hash(value)
    with pytest.raises(ValueError):
        Controller.restore(json.dumps(value).encode())


def test_control_checkpoint_rejects_mutated_live_ledger_and_bad_checksum():
    control = history()
    value = json.loads(control.checkpoint())
    value["state"] = "done"
    with pytest.raises(ValueError, match="checksum"):
        Controller.restore(json.dumps(value).encode())
    control.epochs[1].config["gain"] = 99
    with pytest.raises(ValueError):
        control.checkpoint()


def test_epoch_split_slices_metadata_and_preserves_independent_sample_identity():
    control = Controller()
    domain = ClockDomain(7, 125_000_003, phase_ticks=2**53 + 3)
    control.request("gain", 2, domain.tick_of(2), domain.tick_of(3))
    src = ConvertedSamples(
        np.arange(12, dtype=np.uint16).reshape(6, 2),
        (0, 1),
        0,
        0,
        domain,
        run_id="r",
        branch_id="b",
        session_id=9,
        meta={
            "overflow": np.arange(6) % 2 == 0,
            "capture_time": np.arange(6) + 2**53,
            "per_channel": np.arange(12).reshape(6, 2),
            "label": "source",
        },
    )
    parts = split_by_epoch(src, control)
    assert [(p.first_index, len(p), p.epoch) for p in parts] == [(0, 3, 0), (3, 3, 1)]
    for part in parts:
        assert (part.run_id, part.branch_id, part.session_id) == ("r", "b", 9)
        for name in ("overflow", "capture_time", "per_channel"):
            np.testing.assert_array_equal(
                part.meta[name], src.meta[name][part.first_index : part.first_index + 3]
            )
        assert part.meta["label"] == "source"
    bad = ConvertedSamples(src.codes, src.channels, 0, 0, domain, meta={"overflow": np.zeros(2)})
    with pytest.raises(ValueError, match="metadata"):
        split_by_epoch(bad, control)
    empty = ConvertedSamples(np.empty((0, 2), np.uint16), (0, 1), 0, 0, domain)
    assert split_by_epoch(empty, control) == []


def test_fault_marks_the_epoch_effective_now_instead_of_a_declared_future_epoch():
    control = Controller()
    control.request("gain", 2, 1, 10)
    control.to(State.FAULT, 5)
    assert control.epochs[0].incomplete
    assert not control.epochs[1].incomplete
    assert Controller.restore(control.checkpoint()).checkpoint() == control.checkpoint()


def test_backdated_control_action_is_rejected_before_mutation():
    control = history()
    before = control.checkpoint()
    with pytest.raises(ValueError, match="time order"):
        control.to(State.DRAINING, 1)
    assert control.checkpoint() == before


@pytest.mark.parametrize("state", list(State))
def test_checkpoint_and_reset_of_every_reachable_control_state(state):
    control = Controller()
    route = [
        State.CONFIGURED,
        State.SETTLING,
        State.CALIBRATING,
        State.READY,
        State.SCANNING,
        State.DRAINING,
        State.DONE,
    ]
    if state in (State.FAULT, State.ABORTED):
        control.to(state, 1)
    elif state != State.UNCONFIGURED:
        for target in route:
            control.to(target, 1)
            if target == state:
                break
    saved = control.checkpoint()
    restored = Controller.restore(saved)
    assert restored.state is state
    assert restored.checkpoint() == saved
    for ctl in (control, restored):
        ctl.reset(2)
    assert control.checkpoint() == restored.checkpoint()
    assert control.epochs[0].incomplete == (
        state
        in {
            State.FAULT,
            State.ABORTED,
            State.SCANNING,
            State.DRAINING,
            State.SETTLING,
            State.CALIBRATING,
        }
    )
