"""Configuration presets for the forward chain.

Every number here is an assumption of the preset, not a device value; ``evidence`` says so.
``demo`` is a small, fast, self-consistent scene for tests and demonstrations.
"""

from __future__ import annotations

import numpy as np

from zlscan.engine.chain import ChainConfig, Phase
from zlscan.truth import adc, afe, collect, detector, psf, scan, source
from zlscan.truth.scene import Scene, step_wedge, uniform


def demo_config(seed: int = 1, **over) -> ChainConfig:
    """40 pixels of 2 mm, 4 facets at 6000 rpm (400 Hz), 1 MS/s, 4 fine steps per sample."""
    sc = scan.ScanConfig(facets=4, rpm=6000.0, focal_m=0.1, pitch_x_m=2e-3, pitch_y_m=2e-3, nx=40,
                         x_center_m=0.04, distortion_m=0.0)  # fmt: skip
    cfg = ChainConfig(
        seed=seed,
        fs_hz=1_000_000,
        os=4,
        scan=sc,
        source=source.SourceConfig(power_w=1e-3),
        collect=collect.CollectConfig(eta_pre=0.4, eta_collect=0.02, rolloff=0.0, stray_w=0.0),
        psf=psf.PsfConfig(fwhm_x_m=1.5e-3, fwhm_y_m=1.5e-3),
        detector=detector.DetectorConfig(
            qe=0.15,
            gain=1000.0,
            excess_noise_factor=1.3,
            dark_current_a=1e-9,
            wavelength_nm=635.0,
            anode_limit_a=1e-4,
        ),
        afe=afe.AfeConfig(
            kind="log",
            polarity=-1,
            v0=1.0,
            slope_v_per_dec=0.25,
            i_ref_a=1e-6,
            pole_hz=3e6,
            aa_poles_hz=(2e6,),
        ),
        adc=adc.AdcConfig(bits=16, vmin=0.0, vmax=2.5),
        scenes={"main": step_wedge([0.2, 0.8, 1.4, 2.0], 0.08, 0.2)},
        evidence={"all": "assumed"},
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def nominal_config() -> ChainConfig:
    """Continuous-state research preset for observable calibration and reconstruction."""
    cfg = demo_config()
    cfg.evidence = {"all": "assumed", "preset": "nominal"}
    return cfg


def with_timeline(cfg: ChainConfig, phases: list[Phase], scenes: dict[str, Scene]) -> ChainConfig:
    cfg.timeline = phases
    cfg.scenes = {**cfg.scenes, **scenes}
    return cfg


def clear_scene(width_m: float = 0.08, height_m: float = 0.2) -> Scene:
    return uniform(0.0, width_m, height_m)


def legacy_dc_config(density: float) -> ChainConfig:
    """The cross-comparison conditions of S0-T07 as a noise-free steady-state chain.

    1 mW, pre 0.4, collection 0.00125, QE 0.2, gain 1000, dark 1e-10 A, log law
    -0.95 V + 0.25 V/dec * log10(I / 1e-12), ADC -1..1 V 16 bit. All values are the legacy
    packages' assumptions, not device data.
    """
    cfg = demo_config()
    cfg.scan = scan.ScanConfig(facets=4, rpm=6000.0, focal_m=0.1, pitch_x_m=2e-3, pitch_y_m=2e-3,
                               nx=40, x_center_m=0.04)  # fmt: skip
    cfg.source = source.SourceConfig(power_w=1e-3, gated=False)
    cfg.collect = collect.CollectConfig(eta_pre=0.4, eta_collect=0.00125)
    cfg.psf = psf.PsfConfig(0.0, 0.0)
    cfg.detector = detector.DetectorConfig(qe=0.2, gain=1000.0, excess_noise_factor=1.0,
                                           dark_current_a=1e-10, wavelength_nm=635.0,
                                           shot_noise=False)  # fmt: skip
    cfg.afe = afe.AfeConfig(kind="log", polarity=1, v0=-0.95, slope_v_per_dec=0.25, i_ref_a=1e-12,
                            i_floor_a=1e-12)  # fmt: skip
    cfg.adc = adc.AdcConfig(bits=16, vmin=-1.0, vmax=1.0)
    cfg.scenes = {
        "main": Scene.from_density(np.full((4, 4), density), 0.08, 0.2, off_film_T=10**-density)
    }
    cfg.evidence = {"all": "assumed (legacy cross-comparison conditions)"}
    return cfg


LEGACY_A_NOT_MODELLED = (
    "pmt_bw_hz and pmt_transit_ns (PMT fast response)", "adc_noise_lsb and electronic_noise_v",
    "phantom_supersample (spot is integrated by quadrature instead)",
    "adc_latency_cycles and adc_tpd_ns (belong to the output link)",
)  # fmt: skip
LEGACY_B_NOT_MODELLED = (
    "y_mode step_hold", "line_samples/pre_samples framing", "guard_laser_samples",
    "output_noise_rms_V (electronic noise is stage 5)", "film_to_pmt_path_m",
    "bandwidth_hz and transit_time_ns of the PMT", "aa_order other than the two-pole cascade",
)  # fmt: skip


def from_legacy_a(flat: dict, seed: int = 1) -> tuple[ChainConfig, list[str]]:
    """Package A's flat configuration -> a chain configuration, listing what is not carried over."""
    f = flat
    cfg = demo_config(seed)
    fs = int(f["adc_fs_hz"])
    os = int(f.get("analog_oversample", 8))
    cfg.fs_hz, cfg.os = fs, os * 2 if os % 2 else os
    cfg.scan = scan.ScanConfig(
        facets=int(f["facets"]), rpm=float(f["rpm"]),
        focal_m=0.238732, pitch_x_m=f["pitch_x_um"] * 1e-6, pitch_y_m=f["pitch_y_um"] * 1e-6,
        nx=int(f["nx"]), x_center_m=f["nx"] * f["pitch_x_um"] * 1e-6 / 2,
        active_offset_s=f["active_start_us"] * 1e-6,
    )  # fmt: skip
    cfg.source = source.SourceConfig(power_w=f["laser_power_w"])
    eta_collect = f["eta_collect"] * f["attenuation"]
    cfg.collect = collect.CollectConfig(
        f["eta_pre"],
        eta_collect,
        f.get("collection_rolloff", 0.0),
        f["stray_fraction"] * f["laser_power_w"] * f["eta_pre"] * eta_collect,
    )
    cfg.psf = psf.PsfConfig(f["spot_fwhm_um"] * 1e-6, f["spot_fwhm_um"] * 1e-6)
    cfg.detector = detector.DetectorConfig(f["qe"], f["pmt_gain"], f["pmt_excess_noise_factor"],
                                           f["dark_anode_current_a"], f["wavelength_nm"],
                                           f["pmt_clip_a"], shot_noise=bool(f["shot_noise"]))  # fmt: skip
    cfg.afe = afe.AfeConfig(kind="log", polarity=1, v0=f["log_offset_v"],
                            slope_v_per_dec=f["log_slope_v_dec"], i_ref_a=f["log_i_ref_a"],
                            i_floor_a=f["log_i_floor_a"], pole_hz=f["log_bw_hz"],
                            current_dependent=bool(f["log_bw_current_dependent"]),
                            knee_a=f["log_bw_knee_a"], floor_hz=f["log_bw_floor_hz"],
                            aa_poles_hz=(f["aa_bw_hz"],) if f["aa_bw_hz"] else ())  # fmt: skip
    cfg.adc = adc.AdcConfig(
        f["adc_bits"], f["adc_vmin"], f["adc_vmax"], f["sample_jitter_ps"] * 1e-12
    )
    cfg.legacy_reset_per_line = True
    cfg.evidence = {"all": "assumed (package A configuration)"}
    return cfg, list(LEGACY_A_NOT_MODELLED)


def from_legacy_b(grouped: dict, seed: int | None = None) -> tuple[ChainConfig, list[str]]:
    """Convert package B's grouped configuration with continuous analogue state."""
    g = grouped
    cfg = demo_config(g["seed"] if seed is None else seed)
    sc, op, pm, an, ad = g["scan"], g["optics"], g["pmt"], g["analog"], g["adc"]
    os = int(sc["oversample"])
    cfg.fs_hz, cfg.os = int(sc["fs_hz"]), os * 2 if os % 2 else os
    cfg.scan = scan.ScanConfig(
        facets=6, rpm=10000.0, focal_m=0.238732, pitch_x_m=sc["pitch_x_um"] * 1e-6,
        pitch_y_m=sc["pitch_y_um"] * 1e-6, nx=int(sc["nx"]),
        x_center_m=sc["nx"] * sc["pitch_x_um"] * 1e-6 / 2,
    )  # fmt: skip
    cfg.source = source.SourceConfig(op["laser_power_W"], op["ripple_fraction"], op["ripple_hz"],
                                     op["line_power_sigma"])  # fmt: skip
    cfg.collect = collect.CollectConfig(op["pre_film_efficiency"], op["collection_efficiency"],
                                        op["collection_vignetting"])  # fmt: skip
    sigma = op["spot_sigma_px"] * sc["pitch_x_um"] * 1e-6
    cfg.psf = psf.PsfConfig(sigma * 2.354820045, sigma * 2.354820045)
    cfg.detector = detector.DetectorConfig(pm["qe"], pm["gain"], pm["excess_noise_factor"],
                                           pm["dark_anode_A"], op["wavelength_nm"],
                                           pm["anode_limit_A"], shot_noise=bool(pm["noise_enabled"]))  # fmt: skip
    cfg.afe = afe.AfeConfig(kind=an["mode"], polarity=-1, v0=an["offset_V"],
                            slope_v_per_dec=an["log_slope_V_dec"], i_ref_a=an["reference_current_A"],
                            i_floor_a=an["current_floor_A"], r_t_ohm=an["transimpedance_ohm"],
                            pole_hz=an["log_bandwidth_hz"], driver_gain=an["adc_driver_gain"],
                            driver_offset_v=an["adc_driver_offset_V"],
                            aa_poles_hz=(an["aa_cutoff_hz"],) * 2 if an["aa_cutoff_hz"] else ())  # fmt: skip
    cfg.adc = adc.AdcConfig(ad["bits"], ad["vmin"], ad["vmax"])
    cfg.evidence = {"all": "assumed (package B configuration)"}
    return cfg, list(LEGACY_B_NOT_MODELLED)
