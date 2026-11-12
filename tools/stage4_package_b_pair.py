"""Compare observable edge registration and truth-assisted registration for frozen package B."""

import argparse
import json
import tempfile
from pathlib import Path

from legacy_experiments import run_json
from zlscan.evidence.legacy_runner import stage_package

_SCRIPT = """
import json
import sys
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter

from src.scanner import load_config, run

config = Path(sys.argv[1])
out = Path(sys.argv[2])
cfg = load_config(config)
ny, nx = cfg["scan"]["ny"], cfg["scan"]["nx"]
edge_column = nx // 2
edge_density = np.zeros((ny, nx))
edge_density[:, edge_column:] = 1.0
np.save(out / "target.npy", edge_density)
run(cfg, out / "edge", out / "target.npy")
with np.load(out / "edge/simulation_arrays.npz") as z:
    edge_scan = 10 ** -z["reconstructed"][8:-8]
profile = np.median(edge_scan, axis=0)
half_level = (np.median(profile[8:nx//4]) + np.median(profile[3*nx//4:-8])) / 2
crossings = np.flatnonzero(
    (profile[:-1] >= half_level) & (profile[1:] < half_level)
)
crossing = min(crossings, key=lambda i: abs(i - edge_column))
position = crossing + (profile[crossing] - half_level) / (profile[crossing] - profile[crossing+1])
measured_shift = float(position - (edge_column - 0.5))
variants = {}
for name, enabled in (("noise", True), ("noiseless", False)):
    current = load_config(config)
    if not enabled:
        current["pmt"]["noise_enabled"] = False
        current["analog"]["output_noise_rms_V"] = 0.0
        current["adc"]["clock_jitter_ps"] = 0.0
        current["scan"]["line_jitter_sigma_px"] = 0.0
        current["optics"]["line_power_sigma"] = 0.0
        current["optics"]["ripple_fraction"] = 0.0
    run(current, out / name)
    with np.load(out / name / "simulation_arrays.npz") as z:
        density = z["density"]
        variants[name] = z["reconstructed"]

sigma = cfg["optics"]["spot_sigma_px"]
target = -np.log10(gaussian_filter(10 ** -density, sigma, mode="nearest"))
x = np.arange(nx)
def shifted(image, amount):
    transmittance = 10 ** -image
    return -np.log10([np.interp(x + amount, x, row) for row in transmittance])

region = np.zeros(target.shape, bool)
region[8:-8, 8:-8] = True
mask = region & np.isfinite(target)
for image in variants.values():
    mask &= np.isfinite(image) & np.isfinite(shifted(image, measured_shift))
np.save(out / "mask.npy", mask)

def metrics(image):
    error = (image - target)[mask]
    return {"mse": float(np.mean(error**2)), "bias": float(np.mean(error)),
            "variance": float(np.var(error))}

results = {}
for name, image in variants.items():
    oracle_shift = min(
        np.arange(-6, 6.05, 0.05), key=lambda shift: metrics(shifted(image, shift))["mse"]
    )
    results[name] = {
        "uncorrected": metrics(image),
        "edge_calibrated": metrics(shifted(image, measured_shift)),
        "oracle": metrics(shifted(image, oracle_shift)),
        "oracle_shift_px": float(oracle_shift),
    }
a, b = results["noise"], results["noiseless"]
interaction = a["edge_calibrated"]["mse"] - a["uncorrected"]["mse"] - (
    b["edge_calibrated"]["mse"] - b["uncorrected"]["mse"]
)
print(json.dumps({"edge_calibrated_shift_px": measured_shift,
                  "shared_pixels": int(np.count_nonzero(mask)),
                  "noise_alignment_interaction_od_squared": interaction,
                  "variants": results}))
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="zlscan_stage4_legacy_") as directory:
        package = stage_package("package_b", Path(directory))
        result = {}
        for name in ("demo_log", "ad9268_50m_5x"):
            path = output / name
            path.mkdir()
            result[name] = run_json(
                _SCRIPT, [str(package / f"configs/{name}.json"), str(path)], package
            )
    (output / "run.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
