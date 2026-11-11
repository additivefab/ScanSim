"""Configuration presets for the forward chain.

Every number here is an assumption of the preset, not a device value; ``evidence`` says so.
``demo`` is a small, fast, self-consistent scene for tests and demonstrations.
"""

from __future__ import annotations

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


def with_timeline(cfg: ChainConfig, phases: list[Phase], scenes: dict[str, Scene]) -> ChainConfig:
    cfg.timeline = phases
    cfg.scenes = {**cfg.scenes, **scenes}
    return cfg


def clear_scene(width_m: float = 0.08, height_m: float = 0.2) -> Scene:
    return uniform(0.0, width_m, height_m)
