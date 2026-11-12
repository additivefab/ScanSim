"""Runtime data need explicit packaging declarations; installed-wheel proof is separate."""

import tomllib
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
ASSETS = sorted(
    p
    for p in SRC.rglob("*")
    if p.suffix in {".html", ".json", ".css", ".js", ".svg", ".png"} and p.is_file()
)


@pytest.mark.parametrize("asset", ASSETS, ids=lambda p: str(p.relative_to(SRC)))
def test_each_runtime_asset_is_explicitly_included_in_package_data(asset):
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    covered = False
    for package, patterns in config["tool"]["setuptools"]["package-data"].items():
        directory = SRC.joinpath(*package.split("."))
        if asset.is_relative_to(directory):
            name = asset.relative_to(directory).as_posix()
            covered = covered or any(fnmatchcase(name, p) for p in patterns)
    assert covered, f"runtime asset omitted from explicit package data: {asset.relative_to(SRC)}"
