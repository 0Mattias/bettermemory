//! The datetime spellings and readings of the reference.
//!
//! [`DateTime`] is a Python `datetime.datetime` with a fixed offset or none
//! (the reference only ever builds `timezone`-offset or naive values).
//! [`DateTime::from_isoformat`] is CPython 3.13's C `datetime.fromisoformat`,
//! which reads the UTF-8 bytes of the text, not the code points; the
//! formatters are `isoformat`, `str` and the `strftime` patterns the
//! reference uses; the parsers are the reference's functions over them.

use crate::unicode;

const MAX_YEAR: i64 = 9999;

/// A `datetime.datetime`, naive (`offset_us == None`) or with a fixed
/// UTC offset in microseconds (strictly within one day either way).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DateTime {
    pub year: i32,
    pub month: u32,
    pub day: u32,
    pub hour: u32,
    pub minute: u32,
    pub second: u32,
    pub microsecond: u32,
    pub offset_us: Option<i64>,
}

/// Why a datetime could not be built or read: the exception type.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DateError {
    ValueError,
    OverflowError,
}

impl DateError {
    pub fn name(self) -> &'static str {
        match self {
            DateError::ValueError => "ValueError",
            DateError::OverflowError => "OverflowError",
        }
    }
}

fn is_leap(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

fn days_in_month(year: i64, month: u32) -> u32 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        _ if is_leap(year) => 29,
        _ => 28,
    }
}

/// Days before the year, proleptic Gregorian, from 0001-01-01.
fn days_before_year(year: i64) -> i64 {
    let y = year - 1;
    y * 365 + y / 4 - y / 100 + y / 400
}

fn days_before_month(year: i64, month: u32) -> i64 {
    (1..month).map(|m| i64::from(days_in_month(year, m))).sum()
}

/// The proleptic Gregorian ordinal (0001-01-01 is 1).
fn ymd_to_ord(year: i64, month: u32, day: u32) -> i64 {
    days_before_year(year) + days_before_month(year, month) + i64::from(day)
}

fn ord_to_ymd(ordinal: i64) -> (i64, u32, u32) {
    // Python's _ord2ymd.
    let mut n = ordinal - 1;
    let n400 = n.div_euclid(146_097);
    n = n.rem_euclid(146_097);
    let mut year = n400 * 400 + 1;
    let n100 = n / 36_524;
    n %= 36_524;
    let n4 = n / 1_461;
    n %= 1_461;
    let n1 = n / 365;
    n %= 365;
    year += n100 * 100 + n4 * 4 + n1;
    if n1 == 4 || n100 == 4 {
        return (year - 1, 12, 31);
    }
    let mut month = 1;
    let mut rest = n;
    loop {
        let dim = i64::from(days_in_month(year, month));
        if rest < dim {
            break;
        }
        rest -= dim;
        month += 1;
    }
    (year, month, rest as u32 + 1)
}

impl DateTime {
    /// `datetime(year, ..., tzinfo)`, with the constructor's checks.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        year: i64,
        month: u32,
        day: u32,
        hour: u32,
        minute: u32,
        second: u32,
        microsecond: u32,
        offset_us: Option<i64>,
    ) -> Result<DateTime, DateError> {
        if !(1..=MAX_YEAR).contains(&year)
            || !(1..=12).contains(&month)
            || day < 1
            || day > days_in_month(year, month)
            || hour > 23
            || minute > 59
            || second > 59
            || microsecond > 999_999
        {
            return Err(DateError::ValueError);
        }
        if let Some(offset) = offset_us {
            if offset.unsigned_abs() >= 86_400_000_000 {
                return Err(DateError::ValueError);
            }
        }
        Ok(DateTime {
            year: year as i32,
            month,
            day,
            hour,
            minute,
            second,
            microsecond,
            offset_us,
        })
    }

    /// Microseconds since 0001-01-01T00:00 of the wall-clock fields.
    fn wall_us(&self) -> i128 {
        let days = i128::from(ymd_to_ord(i64::from(self.year), self.month, self.day) - 1);
        ((days * 86_400
            + i128::from(self.hour) * 3600
            + i128::from(self.minute) * 60
            + i128::from(self.second))
            * 1_000_000)
            + i128::from(self.microsecond)
    }

    /// Do the fields name a datetime the constructor accepts? (They are
    /// public, so a value built by hand may not.)
    pub fn is_valid(&self) -> bool {
        DateTime::new(
            i64::from(self.year),
            self.month,
            self.day,
            self.hour,
            self.minute,
            self.second,
            self.microsecond,
            self.offset_us,
        )
        .is_ok()
    }

    fn from_wall_us(total: i128, offset_us: Option<i64>) -> Result<DateTime, DateError> {
        let days = i64::try_from(total.div_euclid(86_400_000_000))
            .map_err(|_| DateError::OverflowError)?;
        let rest = total.rem_euclid(86_400_000_000) as i64;
        let (year, month, day) = ord_to_ymd(days + 1);
        if !(1..=MAX_YEAR).contains(&year) {
            return Err(DateError::OverflowError);
        }
        let seconds = rest / 1_000_000;
        Ok(DateTime {
            year: year as i32,
            month,
            day,
            hour: (seconds / 3600) as u32,
            minute: (seconds / 60 % 60) as u32,
            second: (seconds % 60) as u32,
            microsecond: (rest % 1_000_000) as u32,
            offset_us,
        })
    }

    /// `dt.replace(tzinfo=timezone.utc)` when naive, else `dt`.
    pub fn assume_utc(self) -> DateTime {
        DateTime {
            offset_us: Some(self.offset_us.unwrap_or(0)),
            ..self
        }
    }

    /// `dt.astimezone(timezone.utc)` of an aware value: OverflowError when
    /// the instant falls outside years 1 to 9999. A naive value is read as
    /// the machine's local time by Python; that is not ported (None).
    pub fn to_utc(&self) -> Option<Result<DateTime, DateError>> {
        let offset = self.offset_us?;
        if !self.is_valid() {
            return Some(Err(DateError::ValueError));
        }
        Some(DateTime::from_wall_us(
            self.wall_us() - i128::from(offset),
            Some(0),
        ))
    }

    fn date_text(&self) -> String {
        format!("{:04}-{:02}-{:02}", self.year, self.month, self.day)
    }

    fn time_text(&self) -> String {
        let mut out = format!("{:02}:{:02}:{:02}", self.hour, self.minute, self.second);
        if self.microsecond != 0 {
            out.push_str(&format!(".{:06}", self.microsecond));
        }
        out
    }

    /// The `+HH:MM[:SS[.ffffff]]` Python spells a fixed offset with.
    fn offset_text(offset: i64) -> String {
        let sign = if offset < 0 { '-' } else { '+' };
        let us = offset.unsigned_abs();
        let (seconds, micro) = (us / 1_000_000, us % 1_000_000);
        let mut out = format!("{sign}{:02}:{:02}", seconds / 3600, seconds / 60 % 60);
        if seconds % 60 != 0 || micro != 0 {
            out.push_str(&format!(":{:02}", seconds % 60));
            if micro != 0 {
                out.push_str(&format!(".{micro:06}"));
            }
        }
        out
    }

    /// `dt.isoformat(sep)`.
    pub fn isoformat_sep(&self, sep: char) -> String {
        let mut out = format!("{}{sep}{}", self.date_text(), self.time_text());
        if let Some(offset) = self.offset_us {
            out.push_str(&Self::offset_text(offset));
        }
        out
    }

    /// `dt.isoformat()`.
    pub fn isoformat(&self) -> String {
        self.isoformat_sep('T')
    }

    /// `str(dt)`: `isoformat(sep=" ")`.
    pub fn python_str(&self) -> String {
        self.isoformat_sep(' ')
    }

    /// `repr(dt)`, as `datetime.datetime(...)` with a `timezone`.
    pub fn python_repr(&self) -> String {
        let mut fields = format!(
            "datetime.datetime({}, {}, {}, {}, {}",
            self.year, self.month, self.day, self.hour, self.minute
        );
        if self.second != 0 || self.microsecond != 0 {
            fields.push_str(&format!(", {}", self.second));
        }
        if self.microsecond != 0 {
            fields.push_str(&format!(", {}", self.microsecond));
        }
        match self.offset_us {
            None => {}
            Some(0) => fields.push_str(", tzinfo=datetime.timezone.utc"),
            Some(offset) => {
                let days = offset.div_euclid(86_400_000_000);
                let rest = offset.rem_euclid(86_400_000_000);
                let (seconds, micro) = (rest / 1_000_000, rest % 1_000_000);
                let mut delta = Vec::new();
                if days != 0 {
                    delta.push(format!("days={days}"));
                }
                if seconds != 0 {
                    delta.push(format!("seconds={seconds}"));
                }
                if micro != 0 {
                    delta.push(format!("microseconds={micro}"));
                }
                fields.push_str(&format!(
                    ", tzinfo=datetime.timezone(datetime.timedelta({})))",
                    delta.join(", ")
                ));
                return fields;
            }
        }
        fields.push(')');
        fields
    }

    /// `time_utils.isoformat_utc`: `isoformat()` with every `+00:00`
    /// replaced by `Z` (`str.replace` replaces each occurrence, so an
    /// offset of `+00:00:30` spells `Z:30`).
    pub fn isoformat_utc(&self) -> String {
        self.isoformat().replace("+00:00", "Z")
    }

    /// pydantic's `mode="json"` spelling: `Z` for a zero offset, an offset
    /// as `±HH:MM` of its whole minutes (its seconds dropped).
    pub fn pydantic_json(&self) -> String {
        let mut out = format!("{}T{}", self.date_text(), self.time_text());
        match self.offset_us {
            None => {}
            Some(offset) => {
                let seconds = offset / 1_000_000;
                if seconds == 0 {
                    out.push('Z');
                } else {
                    let sign = if seconds < 0 { '-' } else { '+' };
                    let s = seconds.unsigned_abs();
                    out.push_str(&format!("{sign}{:02}:{:02}", s / 3600, s / 60 % 60));
                }
            }
        }
        out
    }

    /// `strftime` with the `%Y %m %d %H %M %S` fields the reference uses
    /// (and `%%`). `%Y` is zero-padded to four digits, as macOS's C library
    /// pads it; glibc writes a year below 1000 unpadded. Any other
    /// directive is not ported: ValueError.
    pub fn strftime(&self, pattern: &str) -> Result<String, DateError> {
        let mut out = String::new();
        let mut chars = pattern.chars();
        while let Some(c) = chars.next() {
            if c != '%' {
                out.push(c);
                continue;
            }
            match chars.next() {
                Some('Y') => out.push_str(&format!("{:04}", self.year)),
                Some('m') => out.push_str(&format!("{:02}", self.month)),
                Some('d') => out.push_str(&format!("{:02}", self.day)),
                Some('H') => out.push_str(&format!("{:02}", self.hour)),
                Some('M') => out.push_str(&format!("{:02}", self.minute)),
                Some('S') => out.push_str(&format!("{:02}", self.second)),
                Some('%') => out.push('%'),
                Some(_) => return Err(DateError::ValueError),
                None => out.push('%'),
            }
        }
        Ok(out)
    }

    /// The backup and daemon stamp: `astimezone(utc).strftime("%Y%m%dT%H%M%SZ")`.
    pub fn compact_utc(&self) -> Option<Result<String, DateError>> {
        Some(self.to_utc()?.and_then(|u| u.strftime("%Y%m%dT%H%M%SZ")))
    }

    /// `datetime.fromtimestamp(seconds, tz=timezone.utc)` of a double, with
    /// CPython's round-half-even of the fraction to microseconds.
    pub fn from_timestamp_utc(seconds: f64) -> Result<DateTime, DateError> {
        if !seconds.is_finite() {
            return Err(DateError::ValueError);
        }
        let mut whole = seconds.trunc();
        let mut fraction = ((seconds - whole) * 1e6).round_ties_even();
        if fraction >= 1e6 {
            fraction -= 1e6;
            whole += 1.0;
        } else if fraction < 0.0 {
            fraction += 1e6;
            whole -= 1.0;
        }
        // time_t range (64-bit), then the date's range.
        if !(-9.223_372_036_854_776e18..9.223_372_036_854_776e18).contains(&whole) {
            return Err(DateError::OverflowError);
        }
        let epoch_us = (ymd_to_ord(1970, 1, 1) - 1) * 86_400_000_000;
        let total = (whole as i128) * 1_000_000 + fraction as i128 + i128::from(epoch_us);
        DateTime::from_wall_us(total, Some(0)).map_err(|_| DateError::ValueError)
    }

    /// CPython 3.13's C `datetime.fromisoformat(text)`.
    pub fn from_isoformat(text: &[u32]) -> Result<DateTime, DateError> {
        iso::parse(text)
    }
}

mod iso {
    //! A port of `Modules/_datetimemodule.c` (3.13): the parse walks the
    //! UTF-8 bytes of the text, which end in a NUL the C code reads past
    //! its own bounds in two places (kept: `at` returns 0 past the end).

    use super::{DateError, DateTime};

    struct Bytes<'a>(&'a [u8]);

    impl Bytes<'_> {
        fn at(&self, i: usize) -> u8 {
            self.0.get(i).copied().unwrap_or(0)
        }
    }

    fn digits(b: &Bytes, pos: &mut usize, count: usize, value: &mut i64) -> bool {
        for _ in 0..count {
            let d = b.at(*pos).wrapping_sub(b'0');
            *pos += 1;
            if d > 9 {
                return false;
            }
            *value = *value * 10 + i64::from(d);
        }
        true
    }

    fn separator_location(b: &Bytes, len: usize) -> Option<usize> {
        if len == 7 {
            return Some(7);
        }
        if b.at(4) == b'-' {
            if b.at(5) == b'W' {
                if len < 8 {
                    return None;
                }
                if len > 8 && b.at(8) == b'-' {
                    if len == 9 {
                        return None;
                    }
                    if len > 10 && b.at(10).is_ascii_digit() {
                        return Some(8);
                    }
                    return Some(10);
                }
                return Some(8);
            }
            return Some(10);
        }
        if b.at(4) == b'W' {
            let mut idx = 7;
            while idx < len && b.at(idx).is_ascii_digit() {
                idx += 1;
            }
            if idx < 9 {
                return Some(idx);
            }
            return Some(if idx % 2 == 0 { 7 } else { 8 });
        }
        Some(8)
    }

    fn iso_to_ymd(year: i64, week: i64, day: i64) -> Option<(i64, u32, u32)> {
        if !(1..=9999).contains(&year) {
            return None;
        }
        if week <= 0 || week >= 53 {
            let mut out_of_range = true;
            if week == 53 {
                let first_weekday = super::ymd_to_ord(year, 1, 1) % 7;
                if first_weekday == 4 || (first_weekday == 3 && super::is_leap(year)) {
                    out_of_range = false;
                }
            }
            if out_of_range {
                return None;
            }
        }
        if !(1..=7).contains(&day) {
            return None;
        }
        // Monday of ISO week 1.
        let first = super::ymd_to_ord(year, 1, 1);
        let first_weekday = (first + 6) % 7;
        let mut week1_monday = first - first_weekday;
        if first_weekday > 3 {
            week1_monday += 7;
        }
        let ordinal = week1_monday + (week - 1) * 7 + (day - 1);
        let (y, m, d) = super::ord_to_ymd(ordinal);
        Some((y, m, d))
    }

    fn date(b: &Bytes, len: usize) -> Option<(i64, u32, u32)> {
        let mut pos = 0;
        let mut year = 0;
        if !digits(b, &mut pos, 4, &mut year) {
            return None;
        }
        let uses_separator = b.at(pos) == b'-';
        if uses_separator {
            pos += 1;
        }
        if b.at(pos) == b'W' {
            pos += 1;
            let mut week = 0;
            if !digits(b, &mut pos, 2, &mut week) {
                return None;
            }
            let mut day = 0;
            if pos < len {
                if uses_separator {
                    let c = b.at(pos);
                    pos += 1;
                    if c != b'-' {
                        return None;
                    }
                }
                if !digits(b, &mut pos, 1, &mut day) {
                    return None;
                }
            } else {
                day = 1;
            }
            return iso_to_ymd(year, week, day);
        }
        let mut month = 0;
        if !digits(b, &mut pos, 2, &mut month) {
            return None;
        }
        if uses_separator {
            let c = b.at(pos);
            pos += 1;
            if c != b'-' {
                return None;
            }
        }
        let mut day = 0;
        if !digits(b, &mut pos, 2, &mut day) {
            return None;
        }
        Some((year, month as u32, day as u32))
    }

    /// `parse_hh_mm_ss_ff` over `b[start..end]`: Err for a negative
    /// return, Ok(true) for "not at the end of the string".
    fn hh_mm_ss_ff(b: &Bytes, start: usize, end: usize, out: &mut [i64; 4]) -> Result<bool, ()> {
        *out = [0; 4];
        let mut p = start;
        let mut has_separator = true;
        for (i, slot) in out.iter_mut().take(3).enumerate() {
            let mut v = 0;
            if !digits(b, &mut p, 2, &mut v) {
                return Err(());
            }
            *slot = v;
            let c = b.at(p);
            p += 1;
            if i == 0 {
                has_separator = c == b':';
            }
            if p >= end {
                return Ok(c != 0);
            } else if has_separator && c == b':' {
                // After the seconds too: what follows reads as the
                // fraction (`08:09:10:5` is 08:09:10.5).
                continue;
            } else if c == b'.' || c == b',' {
                // On any component: the digits after it are microseconds
                // (`08,5` is 08:00:00.5).
                break;
            } else if !has_separator {
                p -= 1;
            } else {
                return Err(());
            }
        }
        let remains = end.saturating_sub(p);
        let to_parse = remains.min(6);
        let mut micro = 0;
        if !digits(b, &mut p, to_parse, &mut micro) {
            return Err(());
        }
        const CORRECTION: [i64; 5] = [100_000, 10_000, 1_000, 100, 10];
        if to_parse == 0 {
            return Err(());
        }
        if to_parse < 6 {
            micro *= CORRECTION[to_parse - 1];
        }
        out[3] = micro;
        while b.at(p).is_ascii_digit() {
            p += 1;
        }
        Ok(b.at(p) != 0)
    }

    struct Time {
        fields: [i64; 4],
        offset_us: Option<i64>,
    }

    fn time(b: &Bytes, start: usize, end: usize) -> Result<Time, ()> {
        let mut tz = start;
        loop {
            let c = b.at(tz);
            if c == b'Z' || c == b'+' || c == b'-' {
                break;
            }
            tz += 1;
            if tz >= end {
                break;
            }
        }
        let mut fields = [0; 4];
        let rv = hh_mm_ss_ff(b, start, tz, &mut fields)?;
        if tz >= end {
            return if rv {
                Err(())
            } else {
                Ok(Time {
                    fields,
                    offset_us: None,
                })
            };
        }
        if b.at(tz) == b'Z' {
            return if b.at(tz + 1) != 0 {
                Err(())
            } else {
                Ok(Time {
                    fields,
                    offset_us: Some(0),
                })
            };
        }
        let sign = if b.at(tz) == b'-' { -1 } else { 1 };
        let mut tzf = [0; 4];
        let rv = hh_mm_ss_ff(b, tz + 1, end, &mut tzf)?;
        if rv {
            return Err(());
        }
        let seconds = sign * (tzf[0] * 3600 + tzf[1] * 60 + tzf[2]);
        let micro = sign * tzf[3];
        // `tzinfo_from_isoformat_results`: an offset of zero whole seconds
        // is UTC, its microseconds dropped (`-00:00:00.5` reads as UTC).
        let offset_us = if seconds == 0 {
            0
        } else {
            seconds * 1_000_000 + micro
        };
        Ok(Time {
            fields,
            offset_us: Some(offset_us),
        })
    }

    pub(super) fn parse(text: &[u32]) -> Result<DateTime, DateError> {
        if text.len() < 7 {
            return Err(DateError::ValueError);
        }
        // `_sanitize_isoformat_str`: a surrogate at 7, 8 or 10 (the first
        // one there) is read as `T`.
        let mut clean = text.to_vec();
        for pos in [7usize, 8, 10] {
            if pos > clean.len() {
                break;
            }
            if clean
                .get(pos)
                .is_some_and(|&c| (0xD800..0xE000).contains(&c))
            {
                clean[pos] = u32::from(b'T');
                break;
            }
        }
        let Some(utf8) = crate::unicode::to_string(&clean) else {
            return Err(DateError::ValueError);
        };
        let b = Bytes(utf8.as_bytes());
        let len = utf8.len();
        let sep = separator_location(&b, len).ok_or(DateError::ValueError)?;
        let (year, month, day) = date(&b, sep).ok_or(DateError::ValueError)?;
        let mut fields = [0; 4];
        let mut offset_us = None;
        if len > sep {
            // The separator is any one character: the time starts after
            // all of its bytes.
            let width = match b.at(sep) {
                0xF0..=0xFF => 4,
                0xE0..=0xEF => 3,
                0xC0..=0xDF => 2,
                _ => 1,
            };
            let t = time(&b, sep + width, len).map_err(|()| DateError::ValueError)?;
            fields = t.fields;
            offset_us = t.offset_us;
        }
        let in_range = |v: i64, hi: i64| (0..=hi).contains(&v);
        if !in_range(fields[0], 23) || !in_range(fields[1], 59) || !in_range(fields[2], 59) {
            return Err(DateError::ValueError);
        }
        let offset_us = match offset_us {
            Some(0) | None => offset_us,
            Some(o) if o.unsigned_abs() >= 86_400_000_000 => return Err(DateError::ValueError),
            Some(o) => Some(o),
        };
        DateTime::new(
            year,
            month,
            day,
            fields[0] as u32,
            fields[1] as u32,
            fields[2] as u32,
            fields[3] as u32,
            offset_us,
        )
    }
}

fn cps(text: &str) -> Vec<u32> {
    unicode::cps(text)
}

fn replace_all(text: &[u32], from: u32, to: &str) -> Vec<u32> {
    let mut out = Vec::with_capacity(text.len() + 8);
    for &c in text {
        if c == from {
            out.extend(cps(to));
        } else {
            out.push(c);
        }
    }
    out
}

/// `time_utils.parse_event_ts`: one trailing `Z` read as `+00:00`, naive
/// read as UTC, None for text that does not parse.
pub fn parse_event_ts(text: &[u32]) -> Option<DateTime> {
    let mut raw = text.to_vec();
    if raw.last() == Some(&u32::from(b'Z')) {
        raw.pop();
        raw.extend(cps("+00:00"));
    }
    DateTime::from_isoformat(&raw)
        .ok()
        .map(DateTime::assume_utc)
}

/// `store_rows._dt` and `v8._as_dt` of a `str`: every `Z` read as
/// `+00:00`, naive read as UTC; ValueError for text that does not parse.
pub fn parse_record_dt(text: &[u32]) -> Result<DateTime, DateError> {
    DateTime::from_isoformat(&replace_all(text, u32::from(b'Z'), "+00:00"))
        .map(DateTime::assume_utc)
}

fn strip_space(text: &[u32]) -> &[u32] {
    let start = text
        .iter()
        .position(|&c| !unicode::is_space(c))
        .unwrap_or(text.len());
    let end = text
        .iter()
        .rposition(|&c| !unicode::is_space(c))
        .map_or(start, |e| e + 1);
    &text[start..end]
}

/// `origin_history.instant`: a git `%aI` date, surrounding whitespace
/// stripped, naive read as UTC; None for any other text.
pub fn git_instant(text: &[u32]) -> Option<DateTime> {
    DateTime::from_isoformat(strip_space(text))
        .ok()
        .map(DateTime::assume_utc)
}

/// The last second a datetime holds, counted from the epoch.
pub const LAST_SECOND: i64 = 253_402_300_799;

/// `origin_history.seconds`: a git `%at` count of seconds, saturated one
/// past [`LAST_SECOND`], or None.
pub fn git_seconds(text: &[u32]) -> Option<i64> {
    let text = strip_space(text);
    if text.is_empty() || !text.iter().all(|&c| (0x30..=0x39).contains(&c)) {
        return None;
    }
    let digits: String = text.iter().filter_map(|&c| char::from_u32(c)).collect();
    let digits = digits.trim_start_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    let last = LAST_SECOND.to_string();
    if (digits.len(), digits) > (last.len(), last.as_str()) {
        return Some(LAST_SECOND + 1);
    }
    digits.parse().ok()
}

const CROCKFORD: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// `models.is_valid_ulid`: 26 Crockford base-32 capitals (the pattern
/// ends in `\Z`, so no trailing newline passes).
pub fn is_valid_ulid(text: &[u32]) -> bool {
    text.len() == 26
        && text
            .iter()
            .all(|&c| c < 0x80 && CROCKFORD.contains(&(c as u8)))
}

/// `review.ulid_time`: when a ULID was minted, from its first ten
/// characters as milliseconds through `fromtimestamp(ms / 1000, utc)`;
/// None for a value that is not a ULID, ValueError past year 9999 (the
/// result on every platform but Windows, where CPython raises OSError).
pub fn ulid_time(text: &[u32]) -> Result<Option<DateTime>, DateError> {
    if !is_valid_ulid(text) {
        return Ok(None);
    }
    let mut ms: u64 = 0;
    for &c in &text[..10] {
        let Some(index) = CROCKFORD.iter().position(|&x| u32::from(x) == c) else {
            return Ok(None);
        };
        ms = ms * 32 + index as u64;
    }
    DateTime::from_timestamp_utc(ms as f64 / 1000.0).map(Some)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn iso(text: &str) -> Result<String, DateError> {
        DateTime::from_isoformat(&cps(text)).map(|d| d.isoformat())
    }

    #[test]
    fn isoformat_shapes() {
        assert_eq!(iso("2026-11-15"), Ok("2026-11-15T00:00:00".into()));
        assert_eq!(iso("2026-W46-7"), Ok("2026-11-15T00:00:00".into()));
        assert_eq!(iso("2026W467"), Ok("2026-11-15T00:00:00".into()));
        assert_eq!(
            iso("20261115T080910Z"),
            Ok("2026-11-15T08:09:10+00:00".into())
        );
        assert_eq!(
            iso("2026-11-15T08:09:10+02:00:30"),
            Ok("2026-11-15T08:09:10+02:00:30".into())
        );
        assert!(iso("2026-319").is_err());
        assert!(iso("2026-11-15T24:00:00").is_err());
    }

    #[test]
    fn ordinals_round_trip() {
        for ordinal in [1, 365, 366, 719_163, 3_652_059] {
            let (y, m, d) = ord_to_ymd(ordinal);
            assert_eq!(ymd_to_ord(y, m, d), ordinal);
        }
    }
}
