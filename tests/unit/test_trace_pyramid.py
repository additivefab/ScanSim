"""Multi-level min/max summaries: a coarse view must never hide a short pulse or a clipped peak."""

import numpy as np
import pytest

from zlscan.trace.pyramid import Pyramid


def signal(n=100_000, seed=1):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    return (np.sin(t / 900.0) + 0.05 * rng.standard_normal(n)).astype("float32")


def test_levels_shrink_by_the_factor_and_end_small():
    p = Pyramid.build(signal(), base=256, factor=8, min_points=64)
    sizes = [len(level) for level in p.levels]
    assert sizes[0] == -(-100_000 // 256)
    assert all(b <= -(-a // 8) for a, b in zip(sizes, sizes[1:], strict=False))
    assert sizes[-1] >= 1 and sizes[-1] <= 64 * 8


def test_every_level_keeps_the_global_extremes_exactly():
    x = signal()
    p = Pyramid.build(x)
    for level in p.levels:
        assert level[:, 0].min() == x.min() and level[:, 1].max() == x.max()


def test_a_three_sample_spike_survives_to_the_coarsest_level():
    x = signal()
    x[54_321:54_324] += 50.0
    p = Pyramid.build(x)
    assert p.levels[-1][:, 1].max() == x.max() == pytest.approx(x[54_321:54_324].max())
    assert (p.levels[-1][:, 1] > 40).any(), "a mean-based reduction would have averaged this away"


def test_a_clipped_flat_top_is_still_visible():
    x = np.clip(signal() * 2, -1.0, 1.0)
    p = Pyramid.build(x)
    assert p.levels[-1][:, 1].max() == 1.0 and p.levels[-1][:, 0].min() == -1.0


def test_first_and_last_columns_are_the_block_edges():
    x = signal(10_000)
    p = Pyramid.build(x, base=100, factor=4, min_points=8)
    assert p.levels[0][3, 2] == x[300] and p.levels[0][3, 3] == x[399]
    assert p.levels[1][1, 2] == x[400] and p.levels[1][1, 3] == x[799]


def test_the_partial_last_block_is_summarised_not_dropped():
    x = np.arange(1050, dtype="float32")
    p = Pyramid.build(x, base=100, factor=4, min_points=2)
    assert p.levels[0][-1, 0] == 1000 and p.levels[0][-1, 1] == 1049


def test_choosing_a_level_depends_on_samples_per_pixel():
    p = Pyramid.build(signal(), base=256, factor=8, min_points=64)
    assert p.choose_level(span=100_000, pixel_width=100_000) == -1, "one sample per pixel: raw"
    assert p.choose_level(span=100_000, pixel_width=300) == 0
    assert p.choose_level(span=100_000, pixel_width=10) == len(p.levels) - 1


def test_a_query_returns_between_one_and_factor_rows_per_pixel():
    """The chosen level has blocks no larger than a pixel, and levels differ by ``factor``."""
    x = signal(1_000_000)
    p = Pyramid.build(x)
    kind, rows = p.query(start=0, end=1_000_000, pixel_width=1000)
    assert kind == "summary"
    assert 1000 <= len(rows) <= p.factor * 1000 + 2
    assert rows[:, 0].min() == x.min() and rows[:, 1].max() == x.max()


def test_a_narrow_query_falls_back_to_raw_samples():
    x = signal()
    p = Pyramid.build(x)
    kind, rows = p.query(start=500, end=700, pixel_width=1000, raw=x)
    assert kind == "raw" and np.array_equal(rows, x[500:700])


def test_a_narrow_query_without_raw_data_says_so():
    p = Pyramid.build(signal())
    with pytest.raises(LookupError, match="not recorded"):
        p.query(start=500, end=700, pixel_width=1000, raw=None)


def test_nan_in_the_input_is_rejected():
    x = signal()
    x[10] = np.nan
    with pytest.raises(ValueError, match="finite"):
        Pyramid.build(x)
