"""The interval module is bench infrastructure that gates nothing, but it
now prints a verdict word ("no measurable change") next to published
numbers, so its arithmetic is pinned here.

The load-bearing test is `test_census_helpers_are_bit_identical_after_the_move`:
`bench/embed_census.py` produced committed artifacts before this module
existed, and delegating its two helpers is only safe if it is bit-exact.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

_BENCH = Path(__file__).resolve().parents[1] / "bench"
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


interval = _load("bench_interval", _BENCH / "interval.py")


def test_wilson_brackets_the_point_estimate() -> None:
    for hits, total in [(0, 20), (7, 20), (11, 20), (20, 20), (120, 437)]:
        lo, hi = interval.wilson(hits, total)
        assert 0.0 <= lo <= hits / total <= hi <= 1.0


def test_wilson_stays_inside_the_unit_interval_at_the_extremes() -> None:
    """The reason for Wilson over the normal approximation."""
    assert interval.wilson(0, 20)[0] == 0.0
    assert interval.wilson(20, 20)[1] == 1.0


def test_wilson_on_the_dev_instrument_swallows_the_g1_bar() -> None:
    """The audit's headline, pinned so it cannot quietly stop being true."""
    lo, hi = interval.wilson(11, 20)  # the static arm's 55%
    assert lo < 0.60 < hi, "G1's bar should sit inside the incumbent's interval"


def test_wilson_is_empty_for_an_empty_sample() -> None:
    assert interval.wilson(0, 0) == (0.0, 0.0)


def test_mcnemar_ignores_concordant_pairs() -> None:
    """The whole reason to use a paired test: questions both arms got, or
    both missed, carry no information about the difference."""
    a = [True] * 10 + [False] * 6 + [True, True, False, False]
    b = [True] * 10 + [False] * 6 + [False, False, True, True]
    assert interval.read_delta(a, b).p_value == interval.mcnemar_exact(2, 2)


def test_mcnemar_resolution_floor_on_twenty_questions() -> None:
    """Six discordant questions one way is the first p<0.05 — the number
    `bench/POWER_AUDIT.md` and the runner's report both quote."""
    assert interval.mcnemar_exact(5, 0) > 0.05
    assert interval.mcnemar_exact(6, 0) < 0.05


def test_mcnemar_is_symmetric_and_bounded() -> None:
    for a, b in [(0, 0), (3, 1), (1, 3), (9, 0), (0, 9)]:
        p = interval.mcnemar_exact(a, b)
        assert 0.0 <= p <= 1.0
        assert p == interval.mcnemar_exact(b, a)


def test_read_delta_calls_a_one_question_move_unmeasurable() -> None:
    """`bench/retrieval/README.md`'s own rule, made callable."""
    a = [True] * 11 + [False] * 9
    b = [True] * 10 + [False] * 10
    d = interval.read_delta(a, b)
    assert d.questions == 1
    assert d.verdict == "no measurable change"


def test_read_delta_finds_the_requery_effect() -> None:
    """The dev instrument's real, published effect: 16/20 vs 7/20."""
    a = [True] * 16 + [False] * 4
    b = [True] * 7 + [False] * 13
    d = interval.read_delta(a, b)
    assert d.p_value < 0.05
    assert d.verdict.startswith("measurable")


def test_read_delta_refuses_mismatched_arms() -> None:
    with pytest.raises(ValueError, match="same questions"):
        interval.read_delta([True, False], [True])


def test_mean_ci_is_used_for_means_not_proportions() -> None:
    """Macro recall averages fractions; a two-thirds score is one item,
    not two successes out of three."""
    lo, hi = interval.mean_ci([2 / 3] * 500)
    assert lo == pytest.approx(2 / 3) and hi == pytest.approx(2 / 3)
    lo, hi = interval.mean_ci([0.0, 1.0] * 250)
    assert lo < 0.5 < hi


def test_mean_ci_degenerate_inputs() -> None:
    assert interval.mean_ci([]) == (0.0, 0.0)
    assert interval.mean_ci([0.4]) == (0.4, 0.4)


def test_min_n_prices_the_five_point_bar() -> None:
    """Separating 55% from 60% needs a four-figure instrument."""
    assert interval.min_n_for(0.55, 0.60) > 1000
    assert interval.min_n_for(0.35, 0.60) < 100
    assert interval.min_n_for(0.5, 0.5) == 0


def test_inverse_normal_matches_known_quantiles() -> None:
    assert interval._inv_norm(0.975) == pytest.approx(1.959964, abs=1e-6)
    assert interval._inv_norm(0.80) == pytest.approx(0.841621, abs=1e-6)
    assert interval._inv_norm(0.5) == pytest.approx(0.0, abs=1e-9)
    assert interval._inv_norm(0.001) == pytest.approx(-3.090232, abs=1e-5)


def test_inverse_normal_rejects_out_of_range() -> None:
    for bad in (0.0, 1.0, -0.1, 1.1):
        with pytest.raises(ValueError):
            interval._inv_norm(bad)


def test_census_helpers_are_bit_identical_after_the_move() -> None:
    """`embed_census` now delegates. Its committed artifacts were computed
    at the rounded z=1.96, so the delegation must reproduce that exactly —
    not approximately, and not at the shared module's fuller precision."""

    def original_wilson(hits: int, total: int, z: float = 1.96):
        if total <= 0:
            return (0.0, 0.0)
        p = hits / total
        denom = 1.0 + z * z / total
        centre = (p + z * z / (2 * total)) / denom
        spread = (
            z * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total)) / denom
        )
        return (max(0.0, centre - spread), min(1.0, centre + spread))

    for total in (1, 7, 20, 120, 200, 437, 1000):
        for hits in range(0, total + 1, max(1, total // 37)):
            assert original_wilson(hits, total) == interval.wilson(hits, total, 1.96)


def test_paired_difference_is_tighter_than_a_single_arm_interval() -> None:
    """Why `--compare` exists on the LongMemEval runner.

    Between-question difficulty dominates a single arm's spread and
    cancels in the difference, so reading a two-arm gap against one
    arm's interval under-reads real effects — the mirror of the
    over-reading this module was written to stop.
    """
    base = [0.0, 0.5, 1.0, 1.0] * 125
    better = [min(1.0, x + 0.02) for x in base]
    _, plo, phi = interval.paired_mean_diff_ci(better, base)
    slo, shi = interval.mean_ci(better)
    assert (phi - plo) < (shi - slo) / 10
    assert plo > 0.0, "a consistent real improvement should not straddle zero"


def test_paired_difference_straddles_zero_when_arms_agree() -> None:
    same = [0.0, 0.5, 1.0, 1.0] * 125
    diff, lo, hi = interval.paired_mean_diff_ci(same, list(same))
    assert diff == 0.0 and lo <= 0.0 <= hi


def test_paired_difference_refuses_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same items"):
        interval.paired_mean_diff_ci([0.1, 0.2], [0.1])


def test_paired_difference_degenerate_inputs() -> None:
    assert interval.paired_mean_diff_ci([], []) == (0.0, 0.0, 0.0)
    assert interval.paired_mean_diff_ci([0.3], [0.1]) == pytest.approx((0.2, 0.2, 0.2))
