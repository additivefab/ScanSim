"""Why a pixel is invalid (docs/dev/03_contracts.md, section 8). Several can be set at once.

An invalid pixel's density is NaN. It is never clamped to a small positive number and printed as
a high density.
"""

from __future__ import annotations

from enum import IntFlag


class InvalidFlag(IntFlag):
    NONPOSITIVE_TRANSMITTANCE = 1 << 0
    WHITE_REFERENCE_INVALID = 1 << 1
    OUTSIDE_CALIBRATION = 1 << 2
    ADC_SATURATED = 1 << 3
    FRONT_END_CLIPPED = 1 << 4
    MISSING_SAMPLE = 1 << 5
    RESAMPLING_SUPPORT = 1 << 6
    CALIBRATION_MISMATCH = 1 << 7
