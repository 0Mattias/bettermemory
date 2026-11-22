//! `time_utils.py`'s two optional-value helpers. `parse_event_ts` and
//! `isoformat_utc` are [`crate::datetime::parse_event_ts`] and
//! [`DateTime::isoformat_utc`].

use crate::datetime::DateTime;

/// `isoformat_utc_optional(dt)`: None for None, else `isoformat_utc`.
pub fn isoformat_utc_optional(dt: Option<&DateTime>) -> Option<String> {
    dt.map(DateTime::isoformat_utc)
}

/// `ensure_utc(dt)`: a naive value stamped UTC
/// (`replace(tzinfo=timezone.utc)`), an aware one and None as given.
pub fn ensure_utc(dt: Option<DateTime>) -> Option<DateTime> {
    dt.map(DateTime::assume_utc)
}
