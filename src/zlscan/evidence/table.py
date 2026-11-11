"""Load the unified parameter table (src/zlscan/evidence/parameters.json)."""

from __future__ import annotations

import json
from pathlib import Path

from zlscan.contracts.param import Param

DEFAULT_PATH = Path(__file__).with_name("parameters.json")


def load_parameters(path: Path | None = None) -> dict[str, Param]:
    """Map parameter key to its ``Param``. Unknown values stay ``None``."""
    table = json.loads(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
    return {entry["key"]: Param.model_validate(entry["param"]) for entry in table["parameters"]}


def load_names(path: Path | None = None) -> dict[str, str]:
    """Map parameter key to its Chinese display name."""
    table = json.loads(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
    return {entry["key"]: entry["name"] for entry in table["parameters"]}
