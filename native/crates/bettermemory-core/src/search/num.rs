//! The floating-point operations of the ranker that are not one IEEE-754
//! operation: Python's `int / int` (correctly rounded at any size),
//! `round(x, ndigits)` (decimal rounding, half to even, of the exact
//! binary value; `round4` at four digits, `health_model` at three), `max`
//! as Python picks between two floats, and `math.log` with its domain
//! errors.

use super::SearchError;

const TWO_53: u128 = 1 << 53;

/// `x * 2**exp`, exact while the result is a normal double.
fn scale2(mut x: f64, mut exp: i32) -> f64 {
    while exp > 0 {
        let step = exp.min(1000);
        x *= f64::from_bits(((1023 + step) as u64) << 52);
        exp -= step;
    }
    while exp < 0 {
        let step = (-exp).min(1000);
        x *= f64::from_bits(((1023 - step) as u64) << 52);
        exp += step;
    }
    x
}

/// `n / d` of non-negative integers, correctly rounded (half to even), as
/// CPython's `int.__truediv__` gives it. `d` is positive and under 2**72.
fn div_u128(n: u128, d: u128) -> f64 {
    if n == 0 {
        return 0.0;
    }
    if n < TWO_53 && d < TWO_53 {
        // Both exact as doubles: one correctly rounded division.
        return n as f64 / d as f64;
    }
    let ln = 128 - n.leading_zeros() as i32;
    let ld = 128 - d.leading_zeros() as i32;
    // A quotient of 55 or 56 bits: 53 kept, the rest and the remainder
    // decide the rounding.
    let s = 55 - (ln - ld);
    let (num, den) = if s >= 0 { (n << s, d) } else { (n, d << (-s)) };
    let q = num / den;
    let sticky = num % den != 0;
    let bits = 128 - q.leading_zeros() as i32;
    let extra = bits - 53;
    let mut mant = q >> extra;
    let rem = q & ((1u128 << extra) - 1);
    let half = 1u128 << (extra - 1);
    if rem > half || (rem == half && (sticky || mant & 1 == 1)) {
        mant += 1;
    }
    scale2(mant as f64, extra - s)
}

/// Python's `n / d` for integers, `d > 0`, both of magnitude under 2**72.
pub fn int_true_div(n: i128, d: i128) -> f64 {
    debug_assert!(d > 0);
    let q = div_u128(n.unsigned_abs(), d.unsigned_abs());
    if n < 0 {
        -q
    } else {
        q
    }
}

/// `round(x, ndigits)` for `0 <= ndigits <= 15`: CPython rounds the exact
/// binary value of a finite `x` to `ndigits` decimals (half to even,
/// `_Py_dg_dtoa` mode 3) and reads the decimal back (`_Py_dg_strtod`,
/// correctly rounded); an infinity, a NaN and a zero come back as given,
/// and a negative value that rounds to zero is `-0.0`.
///
/// Panics in every build for `ndigits > 15`: the exact arithmetic below
/// holds the scaled mantissa under 2**103 and the divisor under 2**72
/// only that far (from 22 digits it would answer wrongly, where Python's
/// `round` stays exact). Every caller passes 3 or 4.
pub fn round_digits(x: f64, ndigits: u32) -> f64 {
    assert!(
        ndigits <= 15,
        "round_digits holds 0 to 15 digits, not {ndigits}"
    );
    if x == 0.0 || !x.is_finite() {
        return x;
    }
    let bits = x.to_bits();
    let neg = bits >> 63 == 1;
    let biased = ((bits >> 52) & 0x7ff) as i32;
    let frac = bits & ((1 << 52) - 1);
    let (m, e) = if biased == 0 {
        (frac, -1074)
    } else {
        (frac | (1 << 52), biased - 1075)
    };
    if e >= 0 {
        // An integer: any count of decimals holds it whole.
        return x;
    }
    let sh = -e;
    let scale = 10u128.pow(ndigits);
    // Under 2**53 * 10**15 < 2**103.
    let num = u128::from(m) * scale;
    let q = if sh >= 120 {
        // Under 2**103 / 2**120: well below one half of a unit.
        0
    } else {
        let q = num >> sh;
        let rem = num & ((1u128 << sh) - 1);
        let half = 1u128 << (sh - 1);
        if rem > half || (rem == half && q & 1 == 1) {
            q + 1
        } else {
            q
        }
    };
    let r = div_u128(q, scale);
    if neg {
        -r
    } else {
        r
    }
}

/// `round(x, 4)`: [`round_digits`] at four decimals.
pub fn round4(x: f64) -> f64 {
    round_digits(x, 4)
}

/// Python's `max(a, b)` of two floats: `b` only when `b > a`.
pub fn py_max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// `math.log(x)` of a float: ValueError at zero and below (and at -inf),
/// NaN through, else the platform's `log`.
pub fn py_log(x: f64) -> Result<f64, SearchError> {
    if x.is_nan() {
        return Ok(x);
    }
    if x <= 0.0 {
        return Err(SearchError::ValueError);
    }
    Ok(x.ln())
}

/// `math.exp(x)`: OverflowError when a finite `x` overflows.
pub fn py_exp(x: f64) -> Result<f64, SearchError> {
    let r = x.exp();
    if r.is_infinite() && x.is_finite() {
        return Err(SearchError::OverflowError);
    }
    Ok(r)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn division_is_correctly_rounded() {
        assert_eq!(int_true_div(1, 3), 1.0 / 3.0);
        assert_eq!(int_true_div(-7, 2), -3.5);
        // 2**53 + 1 over 1: ties to even, down.
        assert_eq!(int_true_div((1 << 53) + 1, 1), 9007199254740992.0);
        assert_eq!(int_true_div((1 << 53) + 3, 1), 9007199254740996.0);
        // A total_seconds() of a large timedelta: float(n) / 1e6 would
        // round twice.
        let n: i128 = 215_688_849_859_657_881;
        assert_eq!(int_true_div(n, 1_000_000), 215688849859.65787);
        assert_ne!(n as f64 / 1e6, 215688849859.65787);
    }

    #[test]
    fn round4_rounds_half_to_even_on_the_binary_value() {
        assert_eq!(round4(0.03125), 0.0312);
        assert_eq!(round4(0.09375), 0.0938);
        assert_eq!(round4(2.675), 2.675);
        assert_eq!(round4(1.00005), 1.0001);
        assert_eq!(round4(-0.00004), -0.0);
        assert!(round4(-0.00004).is_sign_negative());
        assert_eq!(round4(6.238051680925874), 6.2381);
        assert_eq!(round4(1e300), 1e300);
        assert_eq!(round4(5e-324), 0.0);
    }
}
