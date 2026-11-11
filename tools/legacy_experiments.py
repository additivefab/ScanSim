"""One-off experiments on the legacy packages, now reproducible (S0-T07).

1. step response: the same density step through the same two analogue poles in both packages
   (no anti-alias filter), to measure what the low-pass discretisation difference does;
2. cost per fine step of each package's default configuration (best of three);
3. registration ablation of package B on a fixed region of interest and mask, reported as mean
   squared error split into bias^2 + variance (RMSEs are not additive, so no percentages).

Package B's best shift is found with the truth available, so it is an oracle upper bound on
what alignment can buy, not something a real reconstruction could achieve.
Writes docs/reports/legacy_experiments_data.json.
"""

import json
import sys
import tempfile
from pathlib import Path

from zlscan.evidence import legacy_runner as lr

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/reports/legacy_experiments_data.json"
FS = 20e6
SPP = 2
NX = 64
STEP_COL = 32

A_STEP = """
import json, sys
import numpy as np
import src.scanner_sim as m
NX = %(nx)d; COL = %(col)d
def film(c):
    s = c["phantom_supersample"]
    d = np.zeros((c["ny"] * s, c["nx"] * s)); d[:, COL * s:] = 1.0
    return d, 10.0 ** (-d)
m.make_phantom = film
r = m.run(m.load_config(sys.argv[1]))
t = r["trace"][0]
v = np.asarray(t["sample_valid"]) == 1
print(json.dumps({"t": np.asarray(t["sample_time_s"])[v].tolist(), "v": np.asarray(t["v_adc_diff_v"])[v].tolist()}))
"""

B_STEP = """
import json, sys
from pathlib import Path
import numpy as np
from src.scanner import load_config, run
out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
c = load_config(sys.argv[1]); d = np.zeros((c["scan"]["ny"], c["scan"]["nx"])); d[:, %(col)d:] = 1.0
np.save(out / "d.npy", d); run(c, out, out / "d.npy")
t = np.loadtxt(out / "line_trace.csv", delimiter=",", skiprows=1)
v = t[:, 12] == 1
print(json.dumps({"t": t[v, 0].tolist(), "v": t[v, 10].tolist()}))
"""

B_ABLATION = """
import json, sys
from pathlib import Path
import numpy as np
from scipy.ndimage import gaussian_filter
from src.scanner import load_config, run
cfg_path, out = sys.argv[1], Path(sys.argv[2]); noise = sys.argv[3] == "noise"
c = load_config(cfg_path)
if not noise:
    c["pmt"]["noise_enabled"] = False; c["analog"]["output_noise_rms_V"] = 0.0
    c["adc"]["clock_jitter_ps"] = 0.0; c["scan"]["line_jitter_sigma_px"] = 0.0
    c["optics"]["line_power_sigma"] = 0.0; c["optics"]["ripple_fraction"] = 0.0
run(c, out)
z = np.load(out / "simulation_arrays.npz")
den, rec = z["density"], z["reconstructed"]
sigma = c["optics"]["spot_sigma_px"]
target = -np.log10(gaussian_filter(10.0 ** (-den), sigma, mode="nearest")) if sigma > 0 else den
ny, nx = rec.shape
margin = 8
roi = np.zeros(rec.shape, bool); roi[margin:-margin, margin:-margin] = True
roi &= np.isfinite(rec) & np.isfinite(target)
def stats(img):
    e = (img - target)[roi]
    return {"mse": float(np.mean(e**2)), "bias": float(np.mean(e)), "variance": float(np.var(e)),
            "rmse": float(np.sqrt(np.mean(e**2))), "pixels": int(roi.sum())}
xs = np.arange(nx)
def shifted(s):
    return np.array([np.interp(xs + s, xs, row) for row in rec])
shifts = np.arange(0.0, 6.0, 0.05)
best = min(shifts, key=lambda s: stats(shifted(s))["mse"])
print(json.dumps({"as_is": stats(rec), "oracle_shift_px": float(best), "oracle": stats(shifted(best))}))
"""


def run_json(code: str, args: list[str], cwd: Path) -> dict:
    result = lr.run_python(["-c", code, *args], cwd)
    if result.returncode != 0:
        raise RuntimeError(result.output[-2000:])
    # Libraries may print warnings after the result; the result is the last JSON line.
    lines = [ln for ln in result.output.splitlines() if ln.startswith("{")]
    return json.loads(lines[-1])


def step_response(scratch: Path, pa: Path, pb: Path) -> dict:
    base = lr.read_json(pa / "config/demo.json")
    a = dict(base)
    a.update(nx=NX, ny=4, trace_lines=1, shot_noise=False, adc_noise_lsb=0.0, electronic_noise_v=0.0,
             sample_jitter_ps=0.0, collection_rolloff=0.0, distortion_px=0.0, stray_fraction=0.0,
             spot_fwhm_um=0.0, facet_gain=[1.0] * 6, facet_offset_px=[0.0] * 6, adc_fs_hz=FS,
             samples_per_pixel=SPP, aa_bw_hz=0.0, pmt_bw_hz=8e6, log_bw_hz=3e6,
             optical_path_m=1.0, pmt_transit_ns=20.0, log_bw_current_dependent=False,
             adc_vmin=-1.0, adc_vmax=1.0, continuous_feed=False)  # fmt: skip
    b = json.loads(json.dumps(lr.read_json(pb / "configs/demo_log.json")))
    b["scan"].update(nx=NX, ny=4, fs_hz=FS, line_samples=20000, pre_samples=2000,
                     samples_per_pixel=SPP, distortion_amplitude_px=0.0, line_jitter_sigma_px=0.0)  # fmt: skip
    b["optics"].update(film_to_pmt_path_m=1.0, collection_vignetting=0.0, spot_sigma_px=0.0,
                       line_power_sigma=0.0, ripple_fraction=0.0, laser_power_W=a["laser_power_w"],
                       pre_film_efficiency=a["eta_pre"] * a["eta_collect"] * a["attenuation"],
                       collection_efficiency=1.0)  # fmt: skip
    b["pmt"].update(qe=a["qe"], gain=a["pmt_gain"], dark_anode_A=a["dark_anode_current_a"],
                    bandwidth_hz=8e6, transit_time_ns=20.0, noise_enabled=False)  # fmt: skip
    b["analog"].update(offset_V=a["log_offset_v"], log_slope_V_dec=-a["log_slope_v_dec"],
                       reference_current_A=a["log_i_ref_a"], current_floor_A=a["log_i_floor_a"],
                       log_bandwidth_hz=3e6, aa_cutoff_hz=0.0, output_noise_rms_V=0.0,
                       adc_driver_gain=1.0, adc_driver_offset_V=0.0)  # fmt: skip
    b["adc"].update(vmin=-1.0, vmax=1.0, clock_jitter_ps=0.0)
    (scratch / "sa.json").write_text(json.dumps(a), encoding="utf-8")
    (scratch / "sb.json").write_text(json.dumps(b), encoding="utf-8")
    ra = run_json(A_STEP % {"nx": NX, "col": STEP_COL}, [str(scratch / "sa.json")], pa)
    rb = run_json(
        B_STEP % {"col": STEP_COL}, [str(scratch / "sb.json"), str(scratch / "outsb")], pb
    )
    import numpy as np

    ta, va, tb, vb = (np.array(x) for x in (ra["t"], ra["v"], rb["t"], rb["v"]))
    # Absolute time axis. The two packages choose different valid windows (package A moves its
    # window by the nominal analogue delay), so each trace must NOT be zeroed on its own first
    # valid sample; compare only where the windows overlap.
    lo, hi = max(ta[0], tb[0]), min(ta[-1], tb[-1])
    grid = np.arange(lo, hi, 1 / FS)
    ia, ib = np.interp(grid, ta, va), np.interp(grid, tb, vb)
    shifts_ns = np.arange(-300.0, 300.01, 1.0)
    rms = [
        float(np.sqrt(np.mean((np.interp(grid + s * 1e-9, tb, vb) - ia) ** 2))) for s in shifts_ns
    ]
    k = int(np.argmin(rms))
    swing = float(np.ptp(ia))
    return {
        "samples": len(grid),
        "voltage_swing_v": swing,
        "rms_difference_v": float(np.sqrt(np.mean((ib - ia) ** 2))),
        "max_abs_difference_v": float(np.max(np.abs(ib - ia))),
        "best_time_shift_of_b_ns": float(shifts_ns[k]),
        "rms_after_best_shift_v": rms[k],
    }


def cost_per_step(pa: Path, pb: Path) -> dict:
    code_a = (
        "import time, math, json\nimport src.scanner_sim as m\n"
        "c = m.load_config('config/demo.json'); c['trace_lines'] = 0\n"
        "best = 1e9\nfor _ in range(3):\n    t = time.perf_counter(); m.run(c); best = min(best, time.perf_counter() - t)\n"
        "fs = c['adc_fs_hz']; o = c['analog_oversample']\n"
        "n = math.ceil((c['active_start_us'] + c['guard_us']) * 1e-6 * fs + c['nx'] * c['samples_per_pixel'])"
        " - math.floor((c['active_start_us'] - c['guard_us']) * 1e-6 * fs)\n"
        "lines = c['ny'] + 2 * c['facets']\nsteps = lines * n * o\n"
        "print(json.dumps({'seconds': best, 'substeps': steps, 'ns_per_substep': best / steps * 1e9}))\n"
    )
    code_b = (
        "import time, json, tempfile\nfrom pathlib import Path\nimport src.scanner as s\n"
        "c = s.load_config('configs/ad9268_50m_5x.json')\nbest = 1e9\n"
        "for _ in range(3):\n    with tempfile.TemporaryDirectory() as d:\n"
        "        t = time.perf_counter(); m = s.run(c, Path(d)); best = min(best, time.perf_counter() - t)\n"
        "steps = m['internal_count']\n"
        "print(json.dumps({'seconds': best, 'substeps': steps, 'ns_per_substep': best / steps * 1e9}))\n"
    )
    return {
        "package_a_demo": run_json(code_a, [], pa),
        "package_b_ad9268": run_json(code_b, [], pb),
    }


def ablation(scratch: Path, pb: Path) -> dict:
    out = {}
    for name in ("ad9268_50m_5x", "demo_log"):
        for mode in ("noise", "noiseless"):
            cfg = pb / f"configs/{name}.json"
            out[f"{name}/{mode}"] = run_json(
                B_ABLATION, [str(cfg), str(scratch / f"abl_{name}_{mode}"), mode], pb
            )
    return out


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zlscan_exp_") as tmp:
        scratch = Path(tmp)
        pa = lr.stage_package("package_a", scratch)
        pb = lr.stage_package("package_b", scratch)
        result = {
            "step_response": step_response(scratch, pa, pb),
            "cost_per_step": cost_per_step(pa, pb),
            "registration_ablation_package_b": ablation(scratch, pb),
        }
    OUT.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=1))
    if "--record" in sys.argv:
        from zlscan.evidence.records import compare

        step = result["step_response"]
        criteria = [
            compare("step_response.rms_difference_below_1pct_of_swing", True,
                    step["rms_difference_v"] < 0.01 * step["voltage_swing_v"]),
            compare("cost_per_step.measured_for_both_packages", True,
                    all(v["ns_per_substep"] > 0 for v in result["cost_per_step"].values())),
            compare("ablation.all_cases_finite", True,
                    all(c["as_is"]["mse"] == c["as_is"]["mse"] for c in
                        result["registration_ablation_package_b"].values())),
        ]  # fmt: skip
        print(record_evidence(
            "legacy_experiments", criteria, [OUT],
            "Step response, cost per fine step and registration ablation. The 1 % threshold is a "
            "descriptive choice, not a requirement; the ablation's best shift uses the truth "
            "(oracle upper bound). Timings are best of three on one machine, not a benchmark.",
        ))  # fmt: skip
    return 0


def record_evidence(name: str, criteria: list[dict], outputs: list[Path], notes: str) -> str:
    """Write an evidence record for this run; needs a clean working tree."""
    import time
    from datetime import UTC, datetime

    from zlscan.evidence.records import environment_info, git_state, write_record
    from zlscan.io.manifest import sha256_file

    code = git_state(ROOT)
    if code["dirty"] is not False:
        return "not recorded: the working tree is not clean (commit first)"
    now = datetime.now(UTC)
    inputs = {
        str(p.relative_to(ROOT)): sha256_file(p)
        for p in [ROOT / "legacy/MANIFEST.json", Path(sys.argv[0]).resolve(), *outputs]
    }
    command = {
        "argv": [sys.executable, *sys.argv],
        "cwd": str(ROOT),
        "returncode": 0,
        "seconds": 0.0,
    }
    write_record(
        ROOT / "evidence_runs" / f"{now:%Y-%m-%d}_{name}",
        name=name, status="independent_rerun",
        conclusion="pass" if all(c["passed"] for c in criteria) else "fail",
        commands=[command], inputs=inputs, seed=20261110, criteria=criteria,
        started_at=now, finished_at=datetime.now(UTC),
        log_text="see the data files listed in inputs\n",
        code=code, environment=environment_info(), notes=notes,
    )  # fmt: skip
    _ = time
    return f"recorded evidence_runs/{now:%Y-%m-%d}_{name}"


if __name__ == "__main__":
    sys.exit(main())
