"""Row-addressed scratch storage and bounded-memory NPZ serialization."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO
from zipfile import ZIP_STORED, ZipFile

import numpy as np

from zlscan.contracts.calibration import CalibrationHeader
from zlscan.contracts.flags import InvalidFlag
from zlscan.recon.density import Grid, _contributions, _prepare_transfer, _sample_transmittance


class DiskDensity:
    """Keep only active row buffers in memory, including for nonmonotone vertical mappings."""

    def __init__(
        self,
        scratch: BinaryIO,
        grid: Grid,
        calibration: CalibrationHeader,
        channel: int,
        width: int,
    ) -> None:
        self.grid = grid
        self._scratch = scratch
        self._transfer = _prepare_transfer(calibration, str(channel), width)
        self._row_dtype = np.dtype(
            [
                ("total", "<f8", (grid.width,)),
                ("support", "<f8", (grid.width,)),
                ("flags", "<u2", (grid.width,)),
            ]
        )
        scratch.truncate(grid.height * self._row_dtype.itemsize)

    def _row(self, row: int) -> np.ndarray:
        self._scratch.seek(row * self._row_dtype.itemsize)
        return np.frombuffer(self._scratch.read(self._row_dtype.itemsize), self._row_dtype).copy()

    def add(
        self, codes: np.ndarray, flags: np.ndarray, x: np.ndarray, y: float, gain: float
    ) -> None:
        if not np.isfinite(gain) or gain <= 0:
            raise ValueError("facet gain must be positive and finite")
        transmittance = _sample_transmittance(codes, flags, self._transfer) / gain
        rows = {}
        for px, py, weight, values, reasons in _contributions(
            x, y, self.grid, transmittance, flags
        ):
            # y is constant across one acquired line, so each contribution batch has one row.
            row_index = int(py[0])
            if row_index not in rows:
                rows[row_index] = self._row(row_index)
            row = rows[row_index][0]
            np.bitwise_or.at(row["flags"], px, reasons)
            valid = (reasons == 0) & np.isfinite(values)
            np.add.at(row["support"], px[valid], weight[valid])
            np.add.at(row["total"], px[valid], weight[valid] * values[valid])
        for row_index, row in rows.items():
            self._scratch.seek(row_index * self._row_dtype.itemsize)
            self._scratch.write(row.tobytes())

    def add_linear(
        self,
        transmittance: np.ndarray,
        flags: np.ndarray,
        x: np.ndarray,
        y: np.ndarray | float,
    ) -> None:
        """Deposit already-linearized samples with independently calibrated positions."""
        rows = OrderedDict()
        for px, py, weight, values, reasons in _contributions(
            x, y, self.grid, transmittance, flags
        ):
            for index in np.unique(py):
                row_index = int(index)
                buffer = rows.pop(row_index, None)
                if buffer is None:
                    if len(rows) == 2:
                        evicted_index, evicted = rows.popitem(last=False)
                        self._scratch.seek(evicted_index * self._row_dtype.itemsize)
                        self._scratch.write(evicted.tobytes())
                        del evicted
                    buffer = self._row(row_index)
                rows[row_index] = buffer
                row = buffer[0]
                selected = py == index
                np.bitwise_or.at(row["flags"], px[selected], reasons[selected])
                valid = selected & (reasons == 0) & np.isfinite(values)
                np.add.at(row["support"], px[valid], weight[valid])
                np.add.at(row["total"], px[valid], weight[valid] * values[valid])
        for row_index, row in rows.items():
            self._scratch.seek(row_index * self._row_dtype.itemsize)
            self._scratch.write(row.tobytes())

    def write(self, scratch_dir: Path, output: Path) -> float:
        shape = (self.grid.height, self.grid.width)
        dtypes = {"density": "<f8", "transmittance": "<f8", "flags": "<u2", "support_weight": "<f8"}
        paths = {name: scratch_dir / f"{name}.npy" for name in dtypes}
        valid_pixels = 0
        with ExitStack() as stack:
            files = {name: stack.enter_context(path.open("wb")) for name, path in paths.items()}
            for name, file in files.items():
                np.lib.format.write_array_header_1_0(
                    file, {"descr": dtypes[name], "fortran_order": False, "shape": shape}
                )
            for index in range(self.grid.height):
                row = self._row(index)[0]
                flags, support = row["flags"], row["support"]
                flags[support < 0.5] |= int(InvalidFlag.RESAMPLING_SUPPORT)
                valid = flags == 0
                valid_pixels += int(np.count_nonzero(valid))
                transmittance = np.full(self.grid.width, np.nan, dtype="<f8")
                np.divide(row["total"], support, out=transmittance, where=valid)
                files["density"].write(np.asarray(-np.log10(transmittance), dtype="<f8").tobytes())
                files["transmittance"].write(transmittance.tobytes())
                files["flags"].write(flags.tobytes())
                files["support_weight"].write(support.tobytes())

        output.mkdir(parents=True)
        with ZipFile(output / "density.npz", "x", compression=ZIP_STORED) as archive:
            for path in paths.values():
                archive.write(path, arcname=path.name)
        return valid_pixels / (shape[0] * shape[1])
