//! Python's `float.__repr__`, and `format(value, '.Nf')` and
//! `format(value, 'g')` ([`format_fixed`], [`format_general`]), which
//! round the exact binary value half to even as Rust's `{:.N}` and
//! `{:.Ne}` do.
//!
//! The digits are the shortest decimal string that reads back as the same
//! double, and among those the one closest to it: what CPython's `repr`
//! (David Gay's `dtoa` mode 0) gives, and what Rust's own formatting of an
//! `f64` without a precision gives (Grisu with the exact Dragon4 fallback).
//! Only the layout differs, and this module lays the digits out as
//! CPython's `format_float_short` does for `repr`: positional notation when
//! the decimal point falls within -4 < exponent <= 16, else scientific with
//! a signed exponent of at least two digits, and `.0` added to an integral
//! positional value.

fn split_exp(text: &str) -> (String, i32) {
    // `{:e}` always writes an exponent; a missing one reads as zero.
    let (mantissa, exponent) = text.split_once('e').unwrap_or((text, "0"));
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let exponent: i32 = exponent.parse().unwrap_or(0);
    (digits, exponent + 1)
}

fn parses_to(digits: &str, decpt: i32, value: f64) -> bool {
    format!("0.{digits}e{decpt}").parse::<f64>() == Ok(value)
}

/// The shortest round-trip digits of a finite, non-zero `|value|` and the
/// position of the decimal point: `value == 0.<digits> * 10**decpt`.
///
/// Rust gives the shortest digits nearest the value; when the value lies
/// exactly halfway between two such strings it rounds the tie up, where
/// CPython's `dtoa` takes the one with the even last digit
/// (`254209913874244.625` is `...244.62` in Python). Such a tie is found
/// from the value's exact expansion (Rust's precision formatting is exact)
/// and settled the way `dtoa` settles it.
fn shortest_digits(value: f64) -> (String, i32) {
    let value = value.abs();
    let (digits, decpt) = split_exp(&format!("{value:e}"));
    let n = digits.len();
    // One digit more, correctly rounded: a tie shows as a final 5.
    let (longer, longer_decpt) = split_exp(&format!("{value:.n$e}"));
    if !longer.ends_with('5') || longer_decpt != decpt {
        return (digits, decpt);
    }
    // The exact expansion (a double has fewer than 800 significant digits).
    let (exact, exact_decpt) = split_exp(&format!("{value:.800e}"));
    if exact_decpt != decpt
        || exact.as_bytes()[n] != b'5'
        || exact[n + 1..].bytes().any(|b| b != b'0')
    {
        return (digits, decpt);
    }
    let even = |d: &str| d.ends_with(['0', '2', '4', '6', '8']);
    let down = exact[..n].to_owned();
    if even(&down) && parses_to(&down, decpt, value) {
        return (down, decpt);
    }
    if digits == down && !even(&down) {
        // The tie went down to an odd digit: the even one is a unit up.
        let mut up = down.into_bytes();
        let mut i = up.len();
        loop {
            i -= 1;
            if up[i] == b'9' {
                up[i] = b'0';
                if i == 0 {
                    up.insert(0, b'1');
                    break;
                }
            } else {
                up[i] += 1;
                break;
            }
        }
        let carried = up.len() > n;
        let up: String = up.iter().map(|&b| char::from(b)).collect();
        let up_decpt = decpt + i32::from(carried);
        let up = up.trim_end_matches('0').to_owned();
        if parses_to(&up, up_decpt, value) {
            return (up, up_decpt);
        }
    }
    (digits, decpt)
}

/// `repr(value)`. Infinities and NaN spell as Python spells them
/// (`inf`, `-inf`, `nan`); JSON writers spell those themselves.
pub fn repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_owned();
    }
    if value.is_infinite() {
        return if value > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    let sign = if value.is_sign_negative() { "-" } else { "" };
    if value == 0.0 {
        return format!("{sign}0.0");
    }
    let (digits, decpt) = shortest_digits(value);
    let n = digits.len() as i32;
    let mut out = String::with_capacity(32);
    out.push_str(sign);
    if -4 < decpt && decpt <= 16 {
        if decpt <= 0 {
            out.push_str("0.");
            for _ in 0..-decpt {
                out.push('0');
            }
            out.push_str(&digits);
        } else if decpt >= n {
            out.push_str(&digits);
            for _ in 0..decpt - n {
                out.push('0');
            }
            out.push_str(".0");
        } else {
            out.push_str(&digits[..decpt as usize]);
            out.push('.');
            out.push_str(&digits[decpt as usize..]);
        }
    } else {
        out.push_str(&digits[..1]);
        if n > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let exp = decpt - 1;
        out.push('e');
        out.push(if exp < 0 { '-' } else { '+' });
        let magnitude = exp.unsigned_abs();
        if magnitude < 10 {
            out.push('0');
        }
        out.push_str(&magnitude.to_string());
    }
    out
}

/// The most places `format(value, f".{precision}f")` takes: CPython
/// refuses a precision past `INT_MAX` with ValueError ("precision too
/// big").
pub const MAX_PRECISION: usize = i32::MAX as usize;

/// The places past which every double's expansion holds only zeros: the
/// least subnormal, 2**-1074, has 1,074 after the point.
const EXACT_PLACES: usize = 1074;

/// A precision past [`MAX_PRECISION`], which Python's `format` refuses
/// with ValueError.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PrecisionTooBig;

/// `format(value, f".{precision}f")` (and `"0.2f"`, whose `0` flag pads
/// to no width): the exact binary value rounded half to even at
/// `precision` places, a minus sign on every negative value (`-0.00` for
/// `-0.001` and for `-0.0`), and `nan`, `inf`, `-inf` (a NaN of either
/// sign is `nan`). Rust's `{:.N}` writes the same digits (exact, ties to
/// even); only its `NaN` is spelled otherwise. Past 1,074 places the
/// digits are zeros, written here, since Rust's precision stops at
/// `u16::MAX`.
pub fn format_fixed(value: f64, precision: usize) -> Result<String, PrecisionTooBig> {
    if precision > MAX_PRECISION {
        return Err(PrecisionTooBig);
    }
    if value.is_nan() {
        return Ok("nan".to_owned());
    }
    let places = precision.min(EXACT_PLACES);
    let mut out = format!("{value:.places$}");
    if value.is_finite() {
        out.extend(std::iter::repeat_n('0', precision - places));
    }
    Ok(out)
}

/// `format(value, "g")`: six significant digits, the exact binary value
/// rounded half to even (Rust's `{:.5e}`); positional when the rounded
/// value's decimal exponent is in `-4 <= exp < 6`, else `d.ddddde+XX`
/// with a signed exponent of at least two digits; trailing zeros and a
/// bare point dropped; `0` and `-0` for the zeros, `nan`, `inf`, `-inf`.
/// Rust's `{}` writes `0.00001` and `1000000` where this writes `1e-05`
/// and `1e+06`.
pub fn format_general(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_owned();
    }
    if value.is_infinite() {
        return if value > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    let sign = if value.is_sign_negative() { "-" } else { "" };
    if value == 0.0 {
        return format!("{sign}0");
    }
    let text = format!("{:.5e}", value.abs());
    let (mantissa, exp) = text.split_once('e').unwrap_or((&text, "0"));
    let exp: i64 = exp.parse().unwrap_or(0);
    let digits = mantissa.replace('.', "");
    let digits = digits.trim_end_matches('0');
    let n = digits.len() as i64;
    let mut out = String::from(sign);
    if (-4..6).contains(&exp) {
        if exp < 0 {
            out.push_str("0.");
            out.extend(std::iter::repeat_n('0', (-exp - 1) as usize));
            out.push_str(digits);
        } else if n <= exp + 1 {
            out.push_str(digits);
            out.extend(std::iter::repeat_n('0', (exp + 1 - n) as usize));
        } else {
            let (int, frac) = digits.split_at((exp + 1) as usize);
            out.push_str(int);
            out.push('.');
            out.push_str(frac);
        }
    } else {
        let (lead, rest) = digits.split_at(1);
        out.push_str(lead);
        if !rest.is_empty() {
            out.push('.');
            out.push_str(rest);
        }
        out.push('e');
        out.push(if exp < 0 { '-' } else { '+' });
        out.push_str(&format!("{:02}", exp.unsigned_abs()));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::{format_fixed, format_general, repr, PrecisionTooBig, MAX_PRECISION};

    #[test]
    fn spellings() {
        let cases: &[(f64, &str)] = &[
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (0.1, "0.1"),
            (0.1 + 0.2, "0.30000000000000004"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (123456789012345680.0, "1.2345678901234568e+17"),
            (1e-4, "0.0001"),
            (1e-5, "1e-05"),
            (1e300, "1e+300"),
            (5e-324, "5e-324"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (2.2250738585072014e-308, "2.2250738585072014e-308"),
            (123.456, "123.456"),
        ];
        for &(value, text) in cases {
            assert_eq!(repr(value), text, "{value:e}");
        }
    }

    #[test]
    fn fixed_two_places() {
        let cases: &[(f64, &str)] = &[
            (0.0, "0.00"),
            (-0.0, "-0.00"),
            (-0.001, "-0.00"),
            (0.125, "0.12"),
            (0.375, "0.38"),
            (0.625, "0.62"),
            (0.005, "0.01"),
            (0.015, "0.01"),
            (2.675, "2.67"),
            (0.995, "0.99"),
            (99.995, "100.00"),
            (9.995, "9.99"),
            (1.0, "1.00"),
            (0.5, "0.50"),
            (5e-324, "0.00"),
            (1e22, "10000000000000000000000.00"),
            (f64::NAN, "nan"),
            (-f64::NAN, "nan"),
            (f64::INFINITY, "inf"),
            (f64::NEG_INFINITY, "-inf"),
        ];
        for &(value, text) in cases {
            assert_eq!(format_fixed(value, 2).as_deref(), Ok(text), "{value:e}");
        }
        assert_eq!(format_fixed(2.5, 0).as_deref(), Ok("2"));
        assert_eq!(format_fixed(3.5, 0).as_deref(), Ok("4"));
        assert!(format_fixed(1e300, 2).unwrap_or_default().starts_with("1000000000000000052504760255204420248704468581108159154915854115511802457988908195786371375080447864043704443832883878176942523235360430575644792184786706982848387200926575803737830233794788090059368953234970799945081119038967640880074652742780142494579258788820056842838115669472196386865459400540160.00"));
    }

    /// Python refuses a precision past `INT_MAX` before it reads the
    /// value; past 1,074 places every digit is a zero.
    #[test]
    fn fixed_precision_bounds() {
        for value in [1.0, f64::NAN, f64::INFINITY, 1e300] {
            assert_eq!(format_fixed(value, usize::MAX), Err(PrecisionTooBig));
            assert_eq!(format_fixed(value, usize::MAX - 5), Err(PrecisionTooBig));
            assert_eq!(format_fixed(value, MAX_PRECISION + 1), Err(PrecisionTooBig));
        }
        assert_eq!(MAX_PRECISION, 2_147_483_647);
        let long = format_fixed(1.0, 100_000).unwrap_or_default();
        assert_eq!(long.len(), 100_002);
        assert!(long.starts_with("1.000") && long.ends_with("000"));
        assert_eq!(format_fixed(f64::INFINITY, 100_000).as_deref(), Ok("inf"));
        assert_eq!(format_fixed(f64::NAN, 100_000).as_deref(), Ok("nan"));
        let tiny = format_fixed(5e-324, 1080).unwrap_or_default();
        assert_eq!(tiny.len(), 1082);
        assert!(tiny.starts_with("0.00") && tiny.ends_with("265625000000"));
        let zero = format_fixed(-0.0, 1100).unwrap_or_default();
        assert_eq!(zero.len(), 1103);
        assert!(zero.starts_with("-0.000") && zero.ends_with("000"));
    }

    #[test]
    fn general_six_digits() {
        let cases: &[(f64, &str)] = &[
            (1e-05, "1e-05"),
            (0.0001, "0.0001"),
            (9.99999e-05, "9.99999e-05"),
            (0.000123456789, "0.000123457"),
            (123456.0, "123456"),
            (123456.5, "123456"),
            (1234567.0, "1.23457e+06"),
            (1234565.0, "1.23456e+06"),
            (1234575.0, "1.23458e+06"),
            (999999.5, "1e+06"),
            (9999995.0, "1e+07"),
            (100000.0, "100000"),
            (0.1 + 0.2, "0.3"),
            (0.5, "0.5"),
            (2.5e-5, "2.5e-05"),
            (1e16, "1e+16"),
            (1e300, "1e+300"),
            (1e-300, "1e-300"),
            (5e-324, "4.94066e-324"),
            (0.0, "0"),
            (-0.0, "-0"),
            (-1.5, "-1.5"),
            (f64::NAN, "nan"),
            (-f64::NAN, "nan"),
            (f64::INFINITY, "inf"),
            (f64::NEG_INFINITY, "-inf"),
        ];
        for &(value, text) in cases {
            assert_eq!(format_general(value), text, "{value:e}");
        }
    }
}
