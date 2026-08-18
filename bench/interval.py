"""Intervals and power for the bench instruments — one home, one import.

This module exists because the discipline was already in the repository
and was not reaching the surface that needed it most. `embed_census.py`
has carried a Wilson interval since P1e, with a docstring that states
the reason exactly: an interval "stops the RECORD from over-reading a
number in either direction." That reasoning was applied to census work,
where n is a few hundred emitted terms, and never to the gate reads,
where n is twenty questions and the over-reading risk is far worse.

The numbers that motivated the extraction, all on the twenty-question
dev instrument:

- The static arm's 55% at recall@1 is 11/20, 95% Wilson [0.34, 0.74].
- W1/W1b's G1 bar of 60% is 12/20, 95% Wilson [0.39, 0.78] — the bar
  sits INSIDE the interval of the incumbent it was written to beat.
- Separating 55% from 60% at 80% power needs ~1,500 questions per arm.

None of that makes the published gate verdicts wrong: a bar is a point
comparison and the records that missed theirs missed honestly. What it
makes wrong is READING a one-question move as a finding. The repository
already says so in `bench/retrieval/README.md`, which calls a +5 at
recall@1 "one question out of twenty, read as no measurable change";
this module is that sentence made callable, so a runner can print it
instead of a reader having to remember it.

Nothing here softens a bar. `read_delta` returns prose, not a verdict,
and no caller is permitted to route a gate through it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Z95 = 1.959963984540054


def wilson(hits: int, total: int, z: float = Z95) -> tuple[float, float]:
    """95% Wilson score interval for a proportion.

    Wilson rather than the normal approximation because these
    proportions sit at small n and sometimes near 0 or 1, which is
    exactly where the normal interval misbehaves — it can leave the
    unit interval entirely.

    Byte-identical in behaviour to the copy this was lifted from in
    `embed_census.py`, which continues to import it from here so the
    committed census artifacts stay reproducible.
    """
    if total <= 0:
        return (0.0, 0.0)
    p = hits / total
    denom = 1.0 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    spread = (
        z * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total)) / denom
    )
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def two_proportion_p(hits_a: int, n_a: int, hits_b: int, n_b: int) -> float:
    """Two-sided p for `p_a == p_b`, pooled normal approximation.

    For INDEPENDENT samples. The dev arms are not independent — they
    answer the same twenty questions — so `mcnemar_exact` is the
    correct test there and this one is kept for the census callers it
    was written for, where the two term sets genuinely differ.
    """
    if n_a <= 0 or n_b <= 0:
        return 1.0
    p_pool = (hits_a + hits_b) / (n_a + n_b)
    if p_pool <= 0.0 or p_pool >= 1.0:
        return 1.0
    se = math.sqrt(p_pool * (1.0 - p_pool) * (1.0 / n_a + 1.0 / n_b))
    if se == 0.0:
        return 1.0
    z = (hits_a / n_a - hits_b / n_b) / se
    return math.erfc(abs(z) / math.sqrt(2.0))


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """Two-sided exact McNemar p for two arms on the SAME questions.

    `only_a` is the count of questions arm A got and arm B missed;
    `only_b` the reverse. Questions both arms got, or both missed,
    carry no information about the difference and are correctly
    ignored — which is the whole reason to use this instead of a
    two-proportion test on a paired design.

    This is the test the dev instrument has always wanted. Its arms
    differ by a handful of questions out of twenty, and the paired
    form is the only one with any hope of resolving that: the
    two-proportion test throws away the pairing that is the
    instrument's one statistical advantage.

    Exact rather than the chi-square approximation because the
    discordant counts here are single digits, where the continuity
    correction is doing more work than the data.
    """
    n = only_a + only_b
    if n == 0:
        return 1.0
    # Two-sided exact binomial against p=0.5 on the discordant pairs.
    tail = sum(math.comb(n, i) for i in range(0, min(only_a, only_b) + 1))
    return min(1.0, 2.0 * tail / (2.0**n))


def mean_ci(values: list[float], z: float = Z95) -> tuple[float, float]:
    """Normal CI on the mean of per-item scores.

    For LongMemEval's macro recall, which is a mean of per-question
    FRACTIONS (a question with three evidence sessions can score 1/3),
    not a count of successes. Wilson would be the wrong instrument
    there and would report a falsely tight interval; this is the right
    one, and at n=500 the normal approximation to the mean is sound.
    """
    n = len(values)
    if n == 0:
        return (0.0, 0.0)
    if n == 1:
        return (values[0], values[0])
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    half = z * math.sqrt(var / n)
    return (max(0.0, mean - half), min(1.0, mean + half))


def paired_mean_diff_ci(
    a: list[float], b: list[float], z: float = Z95
) -> tuple[float, float, float]:
    """CI on the MEAN DIFFERENCE between two arms scored on the same items.

    Returns `(mean_diff, lo, hi)` for `a - b`.

    This is the test LongMemEval comparisons need and the one a
    single-arm interval cannot substitute for. Macro recall carries
    large between-question variance — some questions are simply harder
    — and that variance is IDENTICAL in both arms, so it cancels in the
    difference. Reading a two-arm gap against one arm's own ±2 point
    interval therefore overstates the uncertainty badly, in the same
    way that reading a paired dev comparison unpaired understates the
    instrument.

    The direction of the error matters here: using the single-arm
    interval would let a real difference be waved away as noise, which
    is the opposite of the over-reading this module was written to
    stop. Both errors are available and this function avoids one of
    them.
    """
    if len(a) != len(b):
        raise ValueError(f"paired arms must cover the same items: {len(a)} vs {len(b)}")
    diffs = [x - y for x, y in zip(a, b)]
    n = len(diffs)
    if n == 0:
        return (0.0, 0.0, 0.0)
    mean = sum(diffs) / n
    if n == 1:
        return (mean, mean, mean)
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    half = z * math.sqrt(var / n)
    return (mean, mean - half, mean + half)


def min_n_for(p1: float, p2: float, power: float = 0.80, alpha: float = 0.05) -> int:
    """Questions per arm needed to resolve `p1` from `p2`, unpaired.

    Reported next to a missed bar so the miss can be read at the right
    altitude: a bar missed by one question on a twenty-question
    instrument has not been shown to be missed, and this is the number
    that says how far the instrument would have to grow before it
    could show it.
    """
    if p1 == p2:
        return 0
    z_a = _z_for_two_sided(alpha)
    z_b = _z_for_one_sided(1.0 - power)
    p_bar = (p1 + p2) / 2.0
    num = (
        z_a * math.sqrt(2.0 * p_bar * (1.0 - p_bar))
        + z_b * math.sqrt(p1 * (1.0 - p1) + p2 * (1.0 - p2))
    ) ** 2
    return math.ceil(num / ((p1 - p2) ** 2))


def _z_for_two_sided(alpha: float) -> float:
    return _inv_norm(1.0 - alpha / 2.0)


def _z_for_one_sided(beta: float) -> float:
    return _inv_norm(1.0 - beta)


def _inv_norm(p: float) -> float:
    """Inverse standard normal CDF, Acklam's rational approximation.

    Vendored rather than imported because this file must run under the
    zero-dependency bench floor — `statistics.NormalDist` would do, but
    the trainer-side callers pin an older interpreter contract and the
    approximation is accurate to ~1e-9 across the range used here.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p}")
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    ]
    p_low, p_high = 0.02425, 1.0 - 0.02425
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > p_high:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(
            ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        ) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    )


@dataclass(frozen=True)
class DeltaReading:
    """How a difference between two paired arms should be READ.

    `verdict` is prose for a record, never a gate. The gate stays the
    point comparison its declaration wrote; this only stops the prose
    around it from claiming more than the instrument can carry.
    """

    hits_a: int
    hits_b: int
    n: int
    questions: int
    p_value: float
    verdict: str

    def line(self, label_a: str, label_b: str) -> str:
        sign = "+" if self.questions > 0 else ""
        return (
            f"{label_a} vs {label_b}: {sign}{self.questions} question(s) "
            f"of {self.n}, McNemar p={self.p_value:.3f} — {self.verdict}"
        )


def read_delta(
    hits_a: list[bool], hits_b: list[bool], *, alpha: float = 0.05
) -> DeltaReading:
    """Classify a paired arm difference as measurable or not.

    Takes per-question outcomes so the pairing survives — passing two
    aggregate counts here would discard exactly the information that
    makes the comparison worth running.
    """
    if len(hits_a) != len(hits_b):
        raise ValueError(
            f"paired arms must cover the same questions: {len(hits_a)} vs {len(hits_b)}"
        )
    only_a = sum(1 for a, b in zip(hits_a, hits_b) if a and not b)
    only_b = sum(1 for a, b in zip(hits_a, hits_b) if b and not a)
    p = mcnemar_exact(only_a, only_b)
    verdict = "no measurable change" if p > alpha else f"measurable at alpha={alpha}"
    return DeltaReading(
        hits_a=sum(hits_a),
        hits_b=sum(hits_b),
        n=len(hits_a),
        questions=sum(hits_a) - sum(hits_b),
        p_value=p,
        verdict=verdict,
    )
