"""Compare package A and package B link by link under identical physical conditions (S0-T07).

Both models get a spatially constant film density (so timing, blur and geometry drop out), no
noise, no distortion, the same optical efficiency product, the same PMT and the same ADC range.
The log stage's polarity differs between the packages (A: more current, higher voltage; B: the
opposite), so B is given the negated slope, which makes V = off + S*log10(I/Iref) in both.
Then the steady-state quantities along the chain are compared at a sample in the middle of the
active region: detector power, anode current, log-stage voltage, ADC input voltage, ADC code.

Each package runs in a scratch copy in its own subprocess (both are called ``src``).
Writes docs/reports/legacy_cross_compare_data.json; the narrative is written by hand.
"""

import json
import sys
import tempfile
from pathlib import Path

from zlscan.evidence import legacy_runner as lr

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/reports/legacy_cross_compare_data.json"
DENSITIES = (0.0, 1.0, 2.0)

COMMON = dict(laser_w=1e-3, pre=0.4, collect=0.00125, qe=0.2, gain=1000.0, dark_a=1e-10,
              off_v=-0.95, slope=0.25, iref=1e-12, vmin=-1.0, vmax=1.0)  # fmt: skip

A_RUNNER = """
import json, sys
import numpy as np
import src.scanner_sim as m
D = float(sys.argv[1])
def constant_film(c):
    s = c["phantom_supersample"]
    d = np.full((c["ny"] * s, c["nx"] * s), D)
    return d, 10.0 ** (-d)
m.make_phantom = constant_film
r = m.run(m.load_config(sys.argv[2]))
t = r["trace"][0]
valid = np.flatnonzero(np.asarray(t["sample_valid"]) == 1)
k = int(valid[len(valid) // 2])
keys = {"power_w": "p_detector_w", "anode_a": "pmt_anode_a", "v_log": "v_log_v",
        "v_adc": "v_adc_diff_v", "code": "adc_sample_code"}
print(json.dumps({n: float(np.asarray(t[key])[k]) for n, key in keys.items()}))
"""

B_RUNNER = """
import json, sys
from pathlib import Path
import numpy as np
from src.scanner import load_config, run
D = float(sys.argv[1]); out = Path(sys.argv[3]); out.mkdir(parents=True, exist_ok=True)
c = load_config(sys.argv[2])
np.save(out / "d.npy", np.full((c["scan"]["ny"], c["scan"]["nx"]), D))
run(c, out, out / "d.npy")
t = np.loadtxt(out / "line_trace.csv", delimiter=",", skiprows=1)
valid = np.flatnonzero(t[:, 12] == 1)
k = int(valid[len(valid) // 2])
cols = {"power_w": 7, "anode_a": 8, "v_log": 9, "v_adc": 10, "code": 11}
print(json.dumps({n: float(t[k, i]) for n, i in cols.items()}))
"""


def config_a(base: dict) -> dict:
    p = COMMON
    c = dict(base)
    c.update(
        nx=64, ny=8, trace_lines=1, shot_noise=False, adc_noise_lsb=0.0, electronic_noise_v=0.0,
        sample_jitter_ps=0.0, collection_rolloff=0.0, distortion_px=0.0, stray_fraction=0.0,
        spot_fwhm_um=0.0, facet_gain=[1.0] * 6, facet_offset_px=[0.0] * 6,
        laser_power_w=p["laser_w"], eta_pre=p["pre"], eta_collect=p["collect"], attenuation=1.0,
        qe=p["qe"], pmt_gain=p["gain"], dark_anode_current_a=p["dark_a"],
        log_offset_v=p["off_v"], log_slope_v_dec=p["slope"], log_i_ref_a=p["iref"],
        log_i_floor_a=p["iref"], adc_vmin=p["vmin"], adc_vmax=p["vmax"],
    )  # fmt: skip
    return c


def config_b(base: dict) -> dict:
    p = COMMON
    c = json.loads(json.dumps(base))
    c["scan"].update(nx=64, ny=8, distortion_amplitude_px=0.0, line_jitter_sigma_px=0.0)
    c["optics"].update(
        laser_power_W=p["laser_w"], pre_film_efficiency=p["pre"],
        collection_efficiency=p["collect"],
        collection_vignetting=0.0, spot_sigma_px=0.0, line_power_sigma=0.0, ripple_fraction=0.0,
    )  # fmt: skip
    c["pmt"].update(qe=p["qe"], gain=p["gain"], dark_anode_A=p["dark_a"], noise_enabled=False)
    c["analog"].update(
        offset_V=p["off_v"], log_slope_V_dec=-p["slope"], reference_current_A=p["iref"],
        current_floor_A=p["iref"], output_noise_rms_V=0.0, adc_driver_gain=1.0,
        adc_driver_offset_V=0.0,
    )  # fmt: skip
    c["adc"].update(vmin=p["vmin"], vmax=p["vmax"], clock_jitter_ps=0.0)
    return c


def run_json(runner: str, args: list[str], cwd: Path) -> dict:
    result = lr.run_python(["-c", runner, *args], cwd)
    if result.returncode != 0:
        raise RuntimeError(result.output[-2000:])
    # Libraries may print warnings after the result; the result is the last JSON line.
    lines = [ln for ln in result.output.splitlines() if ln.startswith("{")]
    return json.loads(lines[-1])


def main() -> int:
    code = _clean_code_state() if "--record" in sys.argv else None
    rows = []
    with tempfile.TemporaryDirectory(prefix="zlscan_cross_") as tmp:
        scratch = Path(tmp)
        pa = lr.stage_package("package_a", scratch)
        pb = lr.stage_package("package_b", scratch)
        cfg_a, cfg_b = scratch / "a.json", scratch / "b.json"
        cfg_a.write_text(
            json.dumps(config_a(lr.read_json(pa / "config/demo.json"))), encoding="utf-8"
        )
        cfg_b.write_text(
            json.dumps(config_b(lr.read_json(pb / "configs/demo_log.json"))), encoding="utf-8"
        )
        for d in DENSITIES:
            a = run_json(A_RUNNER, [str(d), str(cfg_a)], pa)
            b = run_json(B_RUNNER, [str(d), str(cfg_b), str(scratch / f"outb_{d}")], pb)
            rows.append({"density": d, "a": a, "b": b})
    for row in rows:
        row["relative_difference"] = {
            k: (row["b"][k] - row["a"][k]) / row["a"][k] if row["a"][k] else None for k in row["a"]
        }
        row["absolute_difference"] = {k: row["b"][k] - row["a"][k] for k in row["a"]}
    OUT.write_text(
        json.dumps({"conditions": COMMON, "rows": rows}, indent=1) + "\n", encoding="utf-8"
    )
    for row in rows:
        print(f"D={row['density']}")
        for k in row["a"]:
            print(
                f"  {k:8s} A={row['a'][k]:.9g} B={row['b'][k]:.9g} "
                f"abs={row['absolute_difference'][k]:.3e}"
            )
    if "--record" in sys.argv:
        from zlscan.evidence.records import compare

        criteria = []
        for row in rows:
            for key, value in row["a"].items():
                tol = {"rel_tol": 1e-6} if key != "code" else {}
                criteria.append(compare(f"D={row['density']}.{key}", value, row["b"][key], **tol))
        print(record_evidence(
            "legacy_cross_compare_steady_state", criteria, [OUT],
            "Package B compared with package A at identical constant-density conditions; floats "
            "within relative 1e-6, ADC codes exactly equal. Expectation = package A's value.",
            code,
        ))  # fmt: skip
    return 0


def _clean_code_state() -> dict:
    """Code state BEFORE the run: refuse if the tree is dirty (outputs are written later)."""
    from zlscan.evidence.records import git_state

    code = git_state(ROOT)
    if code["dirty"] is not False:
        raise SystemExit("--record needs a clean working tree; commit first")
    return code


def record_evidence(
    name: str, criteria: list[dict], outputs: list[Path], notes: str, code: dict
) -> str:
    """Write an evidence record for this run; ``code`` is the state taken before it."""
    import time
    from datetime import UTC, datetime

    from zlscan.evidence.records import environment_info, write_record
    from zlscan.io.manifest import sha256_file

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
