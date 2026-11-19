//! `verify_status.py`: how long ago a memory's claims were spot-checked.
//!
//! [`compute_verification_status`] classifies a `last_verified_at` as
//! never, stale or fresh against an injected `now`;
//! [`remote_verification_status`] builds the block a synced record's stamp
//! reads as on a host that never made it. Both answer a
//! [`VerificationStatus`], whose [`VerificationStatus::to_dict`] is the
//! reference's `to_dict`.
//!
//! The arithmetic is the reference's to the bit: `timedelta.total_seconds`
//! is the exact count of microseconds divided by 10^6 and rounded once
//! ([`micros_to_seconds`]), and the day count is CPython's float floor
//! division ([`float_floor_div`]). `stale_after_days` is taken as an `i64`;
//! the reference accepts any `int`, and its callers pass 30.

use crate::datetime::DateTime;
use crate::pyvalue::PyValue;

/// `DEFAULT_VERIFICATION_STALE_DAYS`.
pub const DEFAULT_VERIFICATION_STALE_DAYS: i64 = 30;

/// The block's `status`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Never,
    Stale,
    Fresh,
    Remote,
}

impl Status {
    pub fn name(self) -> &'static str {
        match self {
            Status::Never => "never",
            Status::Stale => "stale",
            Status::Fresh => "fresh",
            Status::Remote => "remote",
        }
    }
}

/// `VerificationStatus`: the frozen dataclass both functions return.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerificationStatus {
    pub status: Status,
    pub last_verified_at: Option<DateTime>,
    pub age_days: Option<i64>,
    pub recommendation: Option<String>,
    pub stale_after_days: i64,
}

impl VerificationStatus {
    /// `to_dict()`: the five keys in the reference's order, the stamp as
    /// `isoformat()` with every `+00:00` replaced by `Z`.
    pub fn to_dict(&self) -> PyValue {
        let opt_str = |v: &Option<String>| match v {
            None => PyValue::None,
            Some(t) => PyValue::str(t),
        };
        PyValue::Dict(vec![
            (PyValue::str("status"), PyValue::str(self.status.name())),
            (
                PyValue::str("last_verified_at"),
                opt_str(&self.last_verified_at.map(|d| d.isoformat_utc())),
            ),
            (
                PyValue::str("age_days"),
                match self.age_days {
                    None => PyValue::None,
                    Some(n) => PyValue::int(i128::from(n)),
                },
            ),
            (
                PyValue::str("recommendation"),
                opt_str(&self.recommendation),
            ),
            (
                PyValue::str("stale_after_days"),
                PyValue::int(i128::from(self.stale_after_days)),
            ),
        ])
    }
}

/// `TypeError: can't subtract offset-naive and offset-aware datetimes`:
/// a naive `now` against a stamp (a naive stamp is made UTC first).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NaiveAwareMix;

/// `_NEVER_RECOMMENDATION`.
pub const NEVER_RECOMMENDATION: &str = "This memory has never been spot-checked since it was \
written. Before relying on any specific claim it makes (file path, commit hash, version, \
configuration, list of items), confirm at least one against ground truth. If the claim still \
holds, call memory_verify(id, note=...) to record the check. If it has drifted, fix the body \
via memory_update first, then memory_verify the corrected memory.";

/// `_stale_recommendation`.
pub fn stale_recommendation(age_days: i64) -> String {
    format!(
        "Last spot-checked {age_days} days ago \u{2014} past the freshness window. Re-confirm \
at least one verifiable claim (path, commit, version, config, list) and call \
memory_verify(id, note=...) to refresh, or memory_update if a claim has drifted."
    )
}

/// `_remote_recommendation`.
pub fn remote_recommendation(last_verified_at: &DateTime) -> String {
    let stamp = last_verified_at.isoformat_utc();
    format!(
        "This memory arrived by sync pull and its verification stamp ({stamp}) was written on \
another host; this host has not checked it, so the stamp is not evidence here. Confirm at \
least one claim against ground truth on this machine and call memory_verify(id, note=..., \
verified_paths=[...]) with paths that resolve here to adopt it, or memory_update if a claim \
has drifted."
    )
}

/// `remote_verification_status(last_verified_at, stale_after_days=...)`.
pub fn remote_verification_status(
    last_verified_at: DateTime,
    stale_after_days: i64,
) -> VerificationStatus {
    let stamp = last_verified_at.assume_utc();
    VerificationStatus {
        status: Status::Remote,
        last_verified_at: Some(stamp),
        age_days: None,
        recommendation: Some(remote_recommendation(&stamp)),
        stale_after_days: stale_after_days.max(0),
    }
}

/// `compute_verification_status(last_verified_at, now=..., stale_after_days=...)`.
pub fn compute_verification_status(
    last_verified_at: Option<DateTime>,
    now: DateTime,
    stale_after_days: i64,
) -> Result<VerificationStatus, NaiveAwareMix> {
    let threshold = stale_after_days.max(0);
    let Some(stamp) = last_verified_at else {
        return Ok(VerificationStatus {
            status: Status::Never,
            last_verified_at: None,
            age_days: None,
            recommendation: Some(NEVER_RECOMMENDATION.to_owned()),
            stale_after_days: threshold,
        });
    };
    let stamp = stamp.assume_utc();
    let (Some(at_now), Some(at_stamp)) = (now.instant_us(), stamp.instant_us()) else {
        return Err(NaiveAwareMix);
    };
    let elapsed = micros_to_seconds(at_now - at_stamp);
    // `max(0.0, x)` keeps the first argument unless x is greater.
    let age_seconds = if elapsed > 0.0 { elapsed } else { 0.0 };
    let age_days = float_floor_div(age_seconds, 86_400.0) as i64;
    let threshold_seconds = i128::from(threshold) * 86_400;
    if float_gt_int(age_seconds, threshold_seconds) {
        return Ok(VerificationStatus {
            status: Status::Stale,
            last_verified_at: Some(stamp),
            age_days: Some(age_days),
            recommendation: Some(stale_recommendation(age_days)),
            stale_after_days: threshold,
        });
    }
    Ok(VerificationStatus {
        status: Status::Fresh,
        last_verified_at: Some(stamp),
        age_days: Some(age_days),
        recommendation: None,
        stale_after_days: threshold,
    })
}

/// Python's `float > int`, exact. `x` is finite and non-negative.
fn float_gt_int(x: f64, n: i128) -> bool {
    if n >= 1 << 53 {
        // x stays below 2^39 (ten thousand years of seconds).
        return x >= 9_007_199_254_740_992.0 && x > n as f64;
    }
    x > n as f64
}

/// `timedelta.total_seconds()`: the integer `us` divided by 10^6, rounded
/// once to nearest, ties to even (Python's `int / int`).
pub fn micros_to_seconds(us: i128) -> f64 {
    if us == 0 {
        return 0.0;
    }
    let magnitude = correctly_rounded_div(us.unsigned_abs(), 1_000_000);
    if us < 0 {
        -magnitude
    } else {
        magnitude
    }
}

fn bit_len(v: u128) -> i32 {
    128 - v.leading_zeros() as i32
}

/// `n / d` rounded to nearest even, for `0 < n < 2^70` and `d < 2^21`.
fn correctly_rounded_div(n: u128, d: u128) -> f64 {
    // A 54-bit quotient: 53 bits and a rounding bit; the remainder is the
    // sticky bit.
    let mut s = 54 + bit_len(d) - bit_len(n);
    let quotient = |s: i32| {
        if s >= 0 {
            ((n << s) / d, (n << s) % d)
        } else {
            let shifted = n >> (-s);
            let lost = n & ((1u128 << (-s)) - 1);
            (shifted / d, (shifted % d) | lost)
        }
    };
    let (mut q, mut r) = quotient(s);
    while q >= 1 << 54 {
        s -= 1;
        (q, r) = quotient(s);
    }
    while q < 1 << 53 {
        s += 1;
        (q, r) = quotient(s);
    }
    let mut mantissa = q >> 1;
    if q & 1 == 1 && (r != 0 || mantissa & 1 == 1) {
        mantissa += 1;
    }
    // mantissa * 2^(1 - s), exact: both factors are representable and
    // the exponent stays in the normal range for the inputs taken here.
    mantissa as f64 * pow2(1 - s)
}

fn pow2(e: i32) -> f64 {
    f64::from_bits(((1023 + e) as u64) << 52)
}

/// CPython's `float.__floordiv__` (`_float_div_mod`), for `wx > 0`.
pub fn float_floor_div(vx: f64, wx: f64) -> f64 {
    // The remainder's own adjustment is not part of the quotient.
    let m = vx % wx;
    let mut div = (vx - m) / wx;
    if m != 0.0 && (wx < 0.0) != (m < 0.0) {
        div -= 1.0;
    }
    if div != 0.0 {
        let mut floordiv = div.floor();
        if div - floordiv > 0.5 {
            floordiv += 1.0;
        }
        floordiv
    } else {
        0.0_f64.copysign(vx / wx)
    }
}
