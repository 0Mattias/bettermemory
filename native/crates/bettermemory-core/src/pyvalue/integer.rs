//! Arithmetic on canonical signed decimal strings, as held by PyValue::Int.

/// Add two big-endian sequences of decimal digits (each in 0..=9).
pub(crate) fn add_digits(a: &[u8], b: &[u8]) -> String {
    let mut out = Vec::with_capacity(a.len().max(b.len()) + 1);
    let (mut i, mut j, mut carry) = (a.len(), b.len(), 0u8);
    while i > 0 || j > 0 || carry > 0 {
        let mut s = carry;
        if i > 0 {
            i -= 1;
            s += a[i];
        }
        if j > 0 {
            j -= 1;
            s += b[j];
        }
        out.push(b'0' + s % 10);
        carry = s / 10;
    }
    out.reverse();
    String::from_utf8(out).unwrap_or_default()
}

/// `|a - b|` of two magnitudes, `a >= b`, as digits without leading zeros.
pub(crate) fn sub_magnitude(a: &str, b: &str) -> String {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    let mut out = Vec::with_capacity(a.len());
    let mut borrow = 0i32;
    for i in 0..a.len() {
        let x = i32::from(a[a.len() - 1 - i] - b'0');
        let y = if i < b.len() {
            i32::from(b[b.len() - 1 - i] - b'0')
        } else {
            0
        };
        let mut d = x - y - borrow;
        borrow = i32::from(d < 0);
        if d < 0 {
            d += 10;
        }
        out.push(b'0' + d as u8);
    }
    while out.len() > 1 && out.last() == Some(&b'0') {
        out.pop();
    }
    out.reverse();
    String::from_utf8(out).unwrap_or_default()
}

/// Sum two canonical signed integers without a machine-word bound.
pub(crate) fn sum(a: &str, b: &str) -> String {
    let (na, a) = a.strip_prefix('-').map_or((false, a), |v| (true, v));
    let (nb, b) = b.strip_prefix('-').map_or((false, b), |v| (true, v));
    let (negative, magnitude) = if na == nb {
        let digits = |v: &str| v.bytes().map(|n| n - b'0').collect::<Vec<_>>();
        (na, add_digits(&digits(a), &digits(b)))
    } else if (a.len(), a) >= (b.len(), b) {
        (na, sub_magnitude(a, b))
    } else {
        (nb, sub_magnitude(b, a))
    };
    if negative && magnitude != "0" {
        format!("-{magnitude}")
    } else {
        magnitude
    }
}
