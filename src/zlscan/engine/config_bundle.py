"""Versioned, non-executable research configuration bundles (ORACLE_ONLY)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, fields
from pathlib import Path

import numpy as np

from zlscan.engine.chain import MODEL_VERSION, ChainConfig, Phase
from zlscan.io.manifest import sha256_file
from zlscan.truth import adc, afe, collect, detector, psf, scan, source
from zlscan.truth.scene import Scene

_COMPONENTS = dict(
    scan=scan.ScanConfig,
    source=source.SourceConfig,
    collect=collect.CollectConfig,
    psf=psf.PsfConfig,
    detector=detector.DetectorConfig,
    afe=afe.AfeConfig,
    adc=adc.AdcConfig,
)


def save_config_bundle(directory: Path, cfg: ChainConfig) -> str:
    directory.mkdir(parents=True, exist_ok=False)
    data = {
        f.name: getattr(cfg, f.name) for f in fields(cfg) if f.name not in {"scenes", "timeline"}
    }
    for name in _COMPONENTS:
        data[name] = asdict(getattr(cfg, name))
    data["timeline"] = [asdict(p) for p in cfg.timeline]
    data["scenes"] = []
    for i, (name, scene) in enumerate(cfg.scenes.items()):
        path = directory / f"scene_{i}.npy"
        np.save(path, scene.transmittance_map, allow_pickle=False)
        metadata = {
            f.name: getattr(scene, f.name) for f in fields(scene) if f.name != "transmittance_map"
        }
        data["scenes"].append(dict(name=name, metadata=metadata, sha256=sha256_file(path)))
    document = dict(
        format="zlscan.chain-bundle/1",
        visibility="ORACLE_ONLY",
        model_version=MODEL_VERSION,
        config_hash=cfg.hash(),
        config=data,
    )
    path = directory / "config.json"
    path.write_text(json.dumps(document, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    return sha256_file(path)


def _construct(cls: type, data: dict):
    if not isinstance(data, dict) or set(data) != {f.name for f in fields(cls)}:
        raise ValueError(f"incomplete or unknown fields for {cls.__name__}")
    try:
        return cls(**data)
    except (TypeError, OverflowError) as exc:
        raise ValueError(f"invalid {cls.__name__}") from exc


@contextmanager
def config_bundle(directory: Path, expected_sha: str) -> Iterator[ChainConfig]:
    path = directory / "config.json"
    if sha256_file(path) != expected_sha:
        raise ValueError("configuration SHA mismatch")
    d = json.loads(path.read_text(encoding="utf-8"))
    if (
        d.get("format") != "zlscan.chain-bundle/1"
        or d.get("visibility") != "ORACLE_ONLY"
        or d.get("model_version") != MODEL_VERSION
    ):
        raise ValueError("unsupported research configuration bundle")
    data = d["config"]
    with ExitStack() as stack:
        scenes = {}
        for i, row in enumerate(data["scenes"]):
            file = directory / f"scene_{i}.npy"
            if sha256_file(file) != row["sha256"]:
                raise ValueError("scene SHA mismatch")
            array = np.load(file, mmap_mode="r", allow_pickle=False)
            stack.callback(array._mmap.close)
            if (
                array.ndim != 2
                or not np.issubdtype(array.dtype, np.number)
                or not np.isfinite(array).all()
            ):
                raise ValueError("invalid scene array")
            if row["name"] in scenes:
                raise ValueError("duplicate scene name")
            scenes[row["name"]] = _construct(Scene, dict(row["metadata"], transmittance_map=array))
        data["scenes"] = scenes
        data["timeline"] = [_construct(Phase, p) for p in data["timeline"]]
        for name, cls in _COMPONENTS.items():
            data[name] = _construct(cls, data[name])
        cfg = _construct(ChainConfig, data)
        if cfg.hash() != d["config_hash"]:
            raise ValueError("configuration content hash mismatch")
        yield cfg


@contextmanager
def configuration_for_run(
    directory: Path, document: dict, override: ChainConfig | None = None
) -> Iterator[ChainConfig]:
    if document.get("model_version") != MODEL_VERSION:
        raise ValueError("saved physical model version is not supported")
    with ExitStack() as stack:
        marker = document.get("workbench")
        if override is not None:
            cfg = override
        elif marker:
            if marker.get("version") != 1 or marker.get("visibility") != "ORACLE_ONLY":
                raise ValueError("unsupported saved workbench schema")
            cfg = stack.enter_context(
                config_bundle(directory / "truth/workbench", marker["config_sha256"])
            )
        else:
            from zlscan.config.presets import demo_config, nominal_config

            cfg = next(
                (
                    c
                    for c in [demo_config(), nominal_config()]
                    if c.hash() == document.get("config_hash")
                ),
                None,
            )
            if cfg is None:
                raise ValueError(
                    "historical configuration unavailable; refusing to guess custom settings"
                )
        if cfg.hash() != document.get("config_hash"):
            raise ValueError("saved configuration hash does not match run")
        yield cfg
