//! The text of a record id (a ULID) as `models.generate_ulid` mints it,
//! and the reads of one the reference has.
//!
//! - [`ulid`]: `generate_ulid` (`models.py:56-64`) from its two inputs, the
//!   milliseconds and the ten random bytes, as Crockford base 32
//!   (`_encode_crockford`, 48-53): ten characters of time, sixteen of
//!   randomness. The reference masks the milliseconds to 48 bits before
//!   writing them (`& ((1 << 48) - 1)`), so a value past 48 bits wraps
//!   rather than being refused; [`ulid`] does the same.
//! - [`millis`]: `int(time.time() * 1000) & ((1 << 48) - 1)` from the
//!   float `time.time()` returned: one IEEE multiplication, truncation
//!   toward zero, then the mask (a negative value wraps as Python's `&`
//!   of a negative int does). Infinite or NaN products are the reference's
//!   OverflowError or ValueError.
//! - [`is_valid_ulid`] (`models.is_valid_ulid`, 71-75, the one in
//!   `records`) and [`ulid_ms`], the milliseconds `review.ulid_time`
//!   (`review.py:1184-1191`) reads from the first ten characters. The
//!   pattern takes only the 32 capitals of [`CROCKFORD`]: no lowercase, no
//!   I, L, O or U, and any of the 32 in the first place, so a valid text
//!   can name up to 50 bits of milliseconds, which [`ulid`] never writes.
//!
//! Pure: no clock and no randomness are read here.

pub use crate::records::is_valid_ulid;

/// Crockford's base-32 alphabet (`models._CROCKFORD`).
pub const CROCKFORD: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// The largest millisecond value an id holds: `(1 << 48) - 1`.
pub const MAX_MS: u64 = (1 << 48) - 1;

/// Why `int(time.time() * 1000)` raises: an infinite product is
/// OverflowError, NaN ValueError.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NotAnInstant {
    Overflow,
    NotANumber,
}

impl NotAnInstant {
    /// The reference's exception type.
    pub fn name(self) -> &'static str {
        match self {
            NotAnInstant::Overflow => "OverflowError",
            NotAnInstant::NotANumber => "ValueError",
        }
    }
}

/// `generate_ulid` for the milliseconds and random bytes it drew: the
/// milliseconds masked to 48 bits, then 26 characters of [`CROCKFORD`].
pub fn ulid(ms: u64, random: &[u8; 10]) -> String {
    let rand = random.iter().fold(0u128, |n, &b| (n << 8) | u128::from(b));
    let mut out = [0u8; 26];
    let mut ms = ms & MAX_MS;
    for slot in out[..10].iter_mut().rev() {
        *slot = CROCKFORD[(ms & 31) as usize];
        ms >>= 5;
    }
    let mut rand = rand;
    for slot in out[10..].iter_mut().rev() {
        *slot = CROCKFORD[(rand & 31) as usize];
        rand >>= 5;
    }
    out.iter().map(|&b| char::from(b)).collect()
}

/// The milliseconds `generate_ulid` takes from `time.time()`'s float:
/// `int(seconds * 1000) & MAX_MS`, exactly as Python computes it for any
/// finite product (however large or negative).
pub fn millis(seconds: f64) -> Result<u64, NotAnInstant> {
    let product = seconds * 1000.0;
    if product.is_nan() {
        return Err(NotAnInstant::NotANumber);
    }
    if product.is_infinite() {
        return Err(NotAnInstant::Overflow);
    }
    let whole = product.trunc();
    // The low 48 bits of the magnitude, then the sign as two's complement.
    let magnitude = whole.abs();
    let low = if magnitude < 9_223_372_036_854_775_808.0 {
        (magnitude as u64) & MAX_MS
    } else {
        // An integer of 2^63 or more: its mantissa shifted left.
        let bits = magnitude.to_bits();
        let exponent = ((bits >> 52) & 0x7ff) as i64 - 1075;
        let mantissa = (bits & ((1 << 52) - 1)) | (1 << 52);
        if exponent >= 48 {
            0
        } else {
            ((u128::from(mantissa) << exponent) as u64) & MAX_MS
        }
    };
    if whole < 0.0 {
        Ok(low.wrapping_neg() & MAX_MS)
    } else {
        Ok(low)
    }
}

/// The milliseconds a valid id names (its first ten characters, up to
/// 50 bits), as `review.ulid_time` reads them; None for a text that is
/// not a ULID.
pub fn ulid_ms(text: &[u32]) -> Option<u64> {
    if !is_valid_ulid(text) {
        return None;
    }
    text[..10].iter().try_fold(0u64, |ms, &c| {
        let digit = CROCKFORD.iter().position(|&x| u32::from(x) == c)?;
        Some(ms * 32 + digit as u64)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_texts() {
        assert_eq!(ulid(0, &[0; 10]), "0".repeat(26));
        assert_eq!(ulid(MAX_MS, &[0xff; 10]), format!("7{}", "Z".repeat(25)));
        assert_eq!(ulid(MAX_MS + 1, &[0; 10]), ulid(0, &[0; 10]));
        assert_eq!(millis(-0.5), Ok(MAX_MS + 1 - 500));
        assert_eq!(millis(f64::NAN), Err(NotAnInstant::NotANumber));
    }
}
