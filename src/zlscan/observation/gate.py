"""Can the available oscilloscope capture the requested signals at the same time?

The photographed instrument has two analogue channels, so three simultaneous signals are
impossible; repeated captures are possible but are not simultaneous and must be labelled so.
"""

from __future__ import annotations

from enum import StrEnum


class CaptureGate(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NON_SIMULTANEOUS = "NON_SIMULTANEOUS"


def measurement_gate(
    channels_required: int, channels_available: int, simultaneous: bool = True
) -> CaptureGate:
    if channels_required <= 0 or channels_available <= 0:
        raise ValueError("channel counts must be positive")
    if simultaneous and channels_required > channels_available:
        return CaptureGate.FAIL
    return CaptureGate.PASS if simultaneous else CaptureGate.NON_SIMULTANEOUS
