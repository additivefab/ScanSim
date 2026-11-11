"""Time is an integer number of picoseconds; periodic events come from a count, not a float."""

from fractions import Fraction

import pytest

from zlscan.contracts.time import PS_PER_S, ClockDomain, PhaseAccumulator, SampleId

# 125 MSPS has an exact 8000 ps period; 20 and 50 MSPS too. 30 MHz does not divide 1e12 ps.
FREQUENCIES = [20_000_000, 50_000_000, 125_000_000, 30_000_000, 33_333_333]


def exact_tick(count: int, hz: int, phase: int = 0) -> Fraction:
    return Fraction(count * PS_PER_S, hz) + phase


@pytest.mark.parametrize("hz", FREQUENCIES)
def test_tick_of_a_billion_counts_is_exact_to_a_picosecond(hz):
    domain = ClockDomain(id=0, freq_num=hz)
    n = 10**9
    exact = exact_tick(n, hz)
    assert abs(domain.tick_of(n) - exact) < 1, "no cumulative drift after 1e9 counts"


@pytest.mark.parametrize("hz", FREQUENCIES)
def test_every_period_is_the_floor_or_the_ceiling_of_the_true_period(hz):
    domain = ClockDomain(id=0, freq_num=hz)
    true_period = Fraction(PS_PER_S, hz)
    lo, hi = int(true_period // 1), int(-(-true_period // 1))
    steps = {domain.tick_of(n + 1) - domain.tick_of(n) for n in range(0, 100_000, 7)}
    assert steps <= {lo, hi}


def test_a_period_that_divides_a_picosecond_evenly_never_jitters():
    domain = ClockDomain(id=0, freq_num=125_000_000)
    assert {domain.tick_of(n + 1) - domain.tick_of(n) for n in range(1000)} == {8000}


def test_a_non_divisible_frequency_has_exactly_two_period_lengths():
    domain = ClockDomain(id=0, freq_num=30_000_000)  # 33333.33 ps
    steps = {domain.tick_of(n + 1) - domain.tick_of(n) for n in range(300)}
    assert steps == {33333, 33334}


def test_phase_offsets_the_whole_domain():
    domain = ClockDomain(id=1, freq_num=50_000_000, phase_ticks=1234)
    assert domain.tick_of(0) == 1234 and domain.tick_of(3) == 3 * 20_000 + 1234


@pytest.mark.parametrize("hz", FREQUENCIES)
def test_count_at_inverts_tick_of(hz):
    domain = ClockDomain(id=0, freq_num=hz, phase_ticks=77)
    for n in (0, 1, 2, 12345, 10**7):
        t = domain.tick_of(n)
        assert domain.count_at(t) == n
        assert domain.count_at(t - 1) == n - 1
        assert domain.count_at(domain.tick_of(n + 1) - 1) == n


def test_a_rational_frequency_is_supported():
    domain = ClockDomain(id=2, freq_num=100_000_000, freq_den=3)  # 33.333... MHz, period 30000 ps
    assert domain.tick_of(3) == 90_000
    n = 3 * 10**9
    assert abs(domain.tick_of(n) - Fraction(n * PS_PER_S * 3, 100_000_000)) < 1


def test_a_non_positive_frequency_is_rejected():
    with pytest.raises(ValueError):
        ClockDomain(id=0, freq_num=0)
    with pytest.raises(ValueError):
        ClockDomain(id=0, freq_num=10, freq_den=0)


# ---- the incremental accumulator agrees with the closed form -------------------------------


@pytest.mark.parametrize("hz", FREQUENCIES)
def test_accumulator_stepping_equals_the_closed_form(hz):
    domain = ClockDomain(id=0, freq_num=hz, phase_ticks=5)
    acc = PhaseAccumulator(domain)
    for n in range(1, 200_001):
        assert acc.step() == domain.tick_of(n), n


@pytest.mark.parametrize("hz", FREQUENCIES)
def test_accumulator_jump_lands_exactly_after_a_billion_counts(hz):
    domain = ClockDomain(id=0, freq_num=hz)
    acc = PhaseAccumulator(domain)
    assert acc.jump(10**9) == domain.tick_of(10**9)
    assert acc.count == 10**9
    assert acc.step() == domain.tick_of(10**9 + 1), "stepping continues without a seam"


def test_accumulator_state_survives_a_checkpoint_round_trip():
    domain = ClockDomain(id=0, freq_num=30_000_000)
    acc = PhaseAccumulator(domain)
    acc.jump(123_456)
    blob = acc.checkpoint()
    other = PhaseAccumulator.restore(domain, blob)
    assert other.step() == acc.step()


# ---- sample identity ------------------------------------------------------------------------


def test_sample_id_has_all_seven_fields_and_is_hashable():
    sid = SampleId("r", "main", 1, 0, 0, 1, 42)
    assert sid._fields == (
        "run_id", "branch_id", "session_id", "epoch", "clock_domain", "channel", "sample_index",
    )  # fmt: skip
    assert {sid: 1}[SampleId("r", "main", 1, 0, 0, 1, 42)] == 1


def test_row_number_cannot_stand_in_for_a_sample_id():
    assert "line" not in SampleId._fields and "facet" not in SampleId._fields
