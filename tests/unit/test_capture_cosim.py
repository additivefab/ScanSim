import shutil

import pytest

from zlscan.digital.cosim import run_capture_cosim


@pytest.mark.skipif(
    not (shutil.which("iverilog") and shutil.which("vvp")),
    reason="Icarus Verilog and vvp are unavailable",
)
def test_icarus_matches_capture_window_reference():
    result = run_capture_cosim()
    assert result["passed"]
    assert set(result["cases"]) == {"ramp", "alternating", "prbs"}
    assert all(
        case["samples"] == 15 and case["ignored_syncs"] == [1] for case in result["cases"].values()
    )
