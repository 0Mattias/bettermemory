//! Python's `float.__repr__`.
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

#[cfg(test)]
mod tests {
    use super::repr;

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
}
