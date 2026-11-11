"""Counter-based random numbers and the exact first-order recursion."""

import numpy as np
import pytest

from zlscan.engine.filters import FirstOrder, reference_response
from zlscan.engine.rng import BLOCK, CounterRng


def test_random_values_do_not_depend_on_how_the_range_is_cut():
    r = CounterRng(5)
    whole = r.uniform("pmt", 1, 0, 3 * BLOCK + 17)
    parts = np.concatenate([r.uniform("pmt", 1, 0, 100), r.uniform("pmt", 1, 100, BLOCK + 50),
                            r.uniform("pmt", 1, BLOCK + 150, 2 * BLOCK - 133)])  # fmt: skip
    assert np.array_equal(whole, parts)


def test_a_fresh_generator_with_the_same_seed_gives_the_same_values():
    assert np.array_equal(
        CounterRng(9).uniform("a", 0, 5000, 300), CounterRng(9).uniform("a", 0, 5000, 300)
    )


def test_different_seeds_sources_and_domains_give_different_streams():
    base = CounterRng(1).uniform("a", 0, 0, 50)
    assert not np.array_equal(base, CounterRng(2).uniform("a", 0, 0, 50))
    assert not np.array_equal(base, CounterRng(1).uniform("b", 0, 0, 50))
    assert not np.array_equal(base, CounterRng(1).uniform("a", 1, 0, 50))


def test_uniforms_are_strictly_inside_the_unit_interval_and_normals_are_standard():
    r = CounterRng(3)
    u = r.uniform("x", 0, 0, 100_000)
    assert 0 < u.min() and u.max() < 1
    z = r.normal("x", 0, 0, 100_000)
    assert abs(z.mean()) < 0.02 and abs(z.std() - 1) < 0.02


def test_the_recursion_matches_its_definition():
    x = np.random.default_rng(0).normal(size=500)
    f = FirstOrder(2e6, 1e-8)
    got = f.process(x)
    want = reference_response(2e6, 1e-8, x)
    assert np.allclose(got[1:], want[1:], atol=1e-9)  # the loop starts from y[0] = x[0] too


def test_direct_current_passes_unchanged():
    f = FirstOrder(1e6, 1e-8)
    y = f.process(np.full(1000, 3.7))
    assert np.allclose(y, 3.7, atol=1e-12)


def test_chunked_filtering_equals_one_pass():
    x = np.random.default_rng(1).normal(size=1000) + 5
    one = FirstOrder(1e6, 1e-8).process(x)
    f = FirstOrder(1e6, 1e-8)
    chunks = np.concatenate([f.process(x[:137]), f.process(x[137:600]), f.process(x[600:])])
    assert np.array_equal(one, chunks)


def test_state_restores_exactly():
    x = np.random.default_rng(2).normal(size=400)
    a = FirstOrder(1e6, 1e-8)
    a.process(x[:200])
    saved = a.state()
    b = FirstOrder(1e6, 1e-8)
    b.set_state(saved)
    assert np.array_equal(a.process(x[200:]), b.process(x[200:]))


def test_step_response_converges_with_a_high_order_as_the_step_shrinks():
    """A linearly rising input has an exact response; the recursion should be nearly exact."""
    fc, slope = 1e6, 1e3
    tau = 1 / (2 * np.pi * fc)
    errors = []
    for dt in (4e-7, 2e-7, 1e-7):
        t = np.arange(0, 2e-5, dt)
        y = FirstOrder(fc, dt).process(slope * t)
        exact = slope * (t - tau)  # steady state lag after the transient
        errors.append(abs(y[-1] - exact[-1]))
    assert max(errors) < 1e-9 * slope


def test_bad_parameters_are_rejected():
    with pytest.raises(ValueError):
        FirstOrder(0, 1e-8)
