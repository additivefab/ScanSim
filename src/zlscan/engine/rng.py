"""Counter-based random numbers (S3-T01): the value for (seed, source, domain, step) never
depends on how the run is cut into chunks.

Uniforms are generated in fixed absolute blocks of ``BLOCK`` steps; a block's generator is keyed
by (seed, source, clock domain, block number). Any distribution is then obtained by an inverse
transform of those uniforms, so a chunk needs no generator state and a checkpoint needs none
either. Different sources (and the two uniforms a Poisson-Gamma draw needs) use different keys.
"""

from __future__ import annotations

import zlib

import numpy as np
from scipy.special import ndtri

BLOCK = 4096


class CounterRng:
    def __init__(self, seed: int) -> None:
        if seed < 0:
            raise ValueError("seed must be non-negative")
        self.seed = seed
        self._cache: dict[tuple[str, int, int], np.ndarray] = {}

    def _block(self, source: str, domain: int, block: int) -> np.ndarray:
        key = (source, domain, block)
        arr = self._cache.get(key)
        if arr is None:
            ss = np.random.SeedSequence([self.seed, zlib.crc32(source.encode()), domain, block])
            arr = np.random.Generator(np.random.PCG64(ss)).random(BLOCK)
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[key] = arr
        return arr

    def uniform(self, source: str, domain: int, start: int, n: int) -> np.ndarray:
        """Uniforms in the open interval (0, 1) for absolute steps ``start .. start+n-1``."""
        if start < 0 or n < 0:
            raise ValueError("start and n must be non-negative")
        out = np.empty(n)
        pos = 0
        while pos < n:
            step = start + pos
            block, off = divmod(step, BLOCK)
            take = min(BLOCK - off, n - pos)
            out[pos : pos + take] = self._block(source, domain, block)[off : off + take]
            pos += take
        return np.clip(out, 1e-16, 1 - 1e-16)

    def normal(self, source: str, domain: int, start: int, n: int) -> np.ndarray:
        return ndtri(self.uniform(source, domain, start, n))
