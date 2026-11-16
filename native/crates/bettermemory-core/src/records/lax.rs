//! pydantic's lax-mode (Python-input) validation of the field types the
//! records use, as the locked pydantic-core (2.14.1's) performs it: `str`,
//! `int`, `bool`, `datetime` (speedate's grammar and its unix timestamps),
//! a `str` enum, and the error `type` each failure names.
//!
//! An input is a Python value the record readers hand the models: a
//! [`PyValue`] (from `json.loads` of a payload or a JSON column, or a value
//! the row readers built) or, from a row, `bytes` (a BLOB).

use crate::datetime::DateTime;
use crate::pyvalue::{PyStr, PyValue};

/// One input value.
#[derive(Clone, Copy, Debug)]
pub enum Arg<'a> {
    Py(&'a PyValue),
    Bytes(&'a [u8]),
}

/// A pydantic error `type`.
pub type Kind = &'static str;

/// The text of a `str` when it holds no lone surrogate.
fn utf8_of(text: &[u32]) -> Option<String> {
    text.iter().map(|&c| char::from_u32(c)).collect()
}

/// A `str` field.
pub fn string(arg: Arg<'_>) -> Result<PyStr, Kind> {
    match arg {
        Arg::Py(PyValue::Str(s)) => Ok(s.clone()),
        Arg::Py(_) => Err("string_type"),
        Arg::Bytes(b) => match std::str::from_utf8(b) {
            Ok(text) => Ok(text.chars().map(u32::from).collect()),
            Err(_) => Err("string_unicode"),
        },
    }
}

/// `str | None`.
pub fn opt_string(arg: Arg<'_>) -> Result<Option<PyStr>, Kind> {
    match arg {
        Arg::Py(PyValue::None) => Ok(None),
        other => string(other).map(Some),
    }
}

/// The text a `str` or `bytes` input is parsed as by the numeric, bool
/// and enum validators; `Err` with the failure's kind when it has none.
fn text_of<'a>(
    arg: Arg<'a>,
    unicode_error: Kind,
) -> Result<Option<std::borrow::Cow<'a, str>>, Kind> {
    match arg {
        Arg::Py(PyValue::Str(s)) => match utf8_of(s) {
            Some(t) => Ok(Some(std::borrow::Cow::Owned(t))),
            None => Err(unicode_error),
        },
        Arg::Bytes(b) => match std::str::from_utf8(b) {
            Ok(t) => Ok(Some(std::borrow::Cow::Borrowed(t))),
            Err(_) => Err(unicode_error),
        },
        Arg::Py(_) => Ok(None),
    }
}

// ---------------------------------------------------------------------------
// int
// ---------------------------------------------------------------------------

/// The longest decimal text `int()` takes (`sys.int_info.default_max_str_digits`).
const MAX_INT_DIGITS: usize = 4300;

/// Decimal digits with single underscores between them, an optional sign
/// first (Python's and num-bigint's shared grammar on ASCII).
fn parse_int_text(text: &str) -> Option<String> {
    let (negative, body) = match text.as_bytes().first() {
        Some(b'-') => (true, &text[1..]),
        Some(b'+') => (false, &text[1..]),
        _ => (false, text),
    };
    if body.is_empty() {
        return None;
    }
    let mut digits = String::with_capacity(body.len());
    let mut previous_underscore = true;
    for b in body.bytes() {
        if b == b'_' {
            if previous_underscore {
                return None;
            }
            previous_underscore = true;
        } else if b.is_ascii_digit() {
            digits.push(b as char);
            previous_underscore = false;
        } else {
            return None;
        }
    }
    if previous_underscore {
        return None;
    }
    let sign = if negative { "-" } else { "" };
    match &PyValue::int_from_decimal(&format!("{sign}{digits}")) {
        Some(PyValue::Int(t)) => Some(t.clone()),
        _ => None,
    }
}

/// pydantic-core's `str_as_int`: trimmed (Unicode White_Space), at most
/// 4300 bytes, an integer, or one followed by `.` and zeros.
fn str_as_int(text: &str) -> Result<String, Kind> {
    let text = text.trim();
    if text.len() > MAX_INT_DIGITS {
        return Err("int_parsing_size");
    }
    if let Some(v) = parse_int_text(text) {
        return Ok(v);
    }
    if let Some(dot) = text.find('.') {
        let fraction = &text[dot + 1..];
        if !fraction.is_empty() && fraction.bytes().all(|b| b == b'0') {
            if let Some(v) = parse_int_text(&text[..dot]) {
                return Ok(v);
            }
        }
    }
    Err("int_parsing")
}

/// 2**63 as a double.
const TWO_63: f64 = 9_223_372_036_854_775_808.0;

/// pydantic-core's `float_as_int`.
fn float_as_int(f: f64) -> Result<String, Kind> {
    if !f.is_finite() {
        Err("finite_number")
    } else if f % 1.0 != 0.0 {
        Err("int_from_float")
    } else if -TWO_63 < f && f < TWO_63 {
        Ok((f as i64).to_string())
    } else {
        Err("int_parsing_size")
    }
}

/// An `int` field: the integer's canonical decimal text.
pub fn int(arg: Arg<'_>) -> Result<String, Kind> {
    match arg {
        Arg::Py(PyValue::Bool(b)) => Ok(if *b { "1" } else { "0" }.to_owned()),
        Arg::Py(PyValue::Int(t)) => Ok(t.clone()),
        Arg::Py(PyValue::Float(f)) => float_as_int(*f),
        Arg::Py(PyValue::Str(_)) | Arg::Bytes(_) => match text_of(arg, "string_unicode")? {
            Some(t) => str_as_int(&t),
            None => Err("int_parsing"),
        },
        Arg::Py(_) => Err("int_type"),
    }
}

// ---------------------------------------------------------------------------
// bool
// ---------------------------------------------------------------------------

/// A `bool` field.
pub fn boolean(arg: Arg<'_>) -> Result<bool, Kind> {
    match arg {
        Arg::Py(PyValue::Bool(b)) => Ok(*b),
        Arg::Py(PyValue::Int(t)) => match t.parse::<i64>() {
            Ok(0) => Ok(false),
            Ok(1) => Ok(true),
            Ok(_) => Err("bool_parsing"),
            Err(_) => Err("bool_type"),
        },
        Arg::Py(PyValue::Float(f)) => match float_as_int(*f) {
            Ok(t) if t == "0" => Ok(false),
            Ok(t) if t == "1" => Ok(true),
            Ok(_) => Err("bool_parsing"),
            Err(_) => Err("bool_type"),
        },
        Arg::Py(PyValue::Str(_)) | Arg::Bytes(_) => {
            let Some(text) = text_of(arg, "string_unicode")? else {
                return Err("bool_parsing");
            };
            match text.to_ascii_lowercase().as_str() {
                "0" | "off" | "f" | "false" | "n" | "no" => Ok(false),
                "1" | "on" | "t" | "true" | "y" | "yes" => Ok(true),
                _ => Err("bool_parsing"),
            }
        }
        Arg::Py(_) => Err("bool_type"),
    }
}

// ---------------------------------------------------------------------------
// str enums
// ---------------------------------------------------------------------------

/// A `str` enum field: the member whose value the input spells.
pub fn enum_member<T: Copy>(arg: Arg<'_>, members: &[(&str, T)]) -> Result<T, Kind> {
    let text = match arg {
        Arg::Py(PyValue::Str(s)) => Some(utf8_of(s).ok_or("string_unicode")?),
        Arg::Bytes(b) => std::str::from_utf8(b).ok().map(str::to_owned),
        Arg::Py(_) => None,
    };
    text.and_then(|t| members.iter().find(|(v, _)| *v == t).map(|(_, m)| *m))
        .ok_or("enum")
}

// ---------------------------------------------------------------------------
// datetime
// ---------------------------------------------------------------------------

/// speedate's datetime before Python builds it: the year may be 0.
#[derive(Clone, Copy, Debug)]
struct Fields {
    year: i64,
    month: u32,
    day: u32,
    hour: u32,
    minute: u32,
    second: u32,
    microsecond: u32,
    offset_seconds: Option<i64>,
}

fn is_leap(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

fn days_in(year: i64, month: u32) -> u32 {
    match month {
        2 if is_leap(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

fn digits2(b: &[u8], at: usize) -> Option<u32> {
    let (x, y) = (*b.get(at)?, *b.get(at + 1)?);
    (x.is_ascii_digit() && y.is_ascii_digit())
        .then(|| u32::from(x - b'0') * 10 + u32::from(y - b'0'))
}

/// speedate's RFC 3339 reading (`DateTime::parse_bytes_rfc3339`), with
/// fractional digits past six truncated.
fn rfc3339(b: &[u8]) -> Option<Fields> {
    if b.len() < 10 || !b[..4].iter().all(u8::is_ascii_digit) || b[4] != b'-' || b[7] != b'-' {
        return None;
    }
    let year = b[..4]
        .iter()
        .fold(0i64, |a, &d| a * 10 + i64::from(d - b'0'));
    let month = digits2(b, 5)?;
    let day = digits2(b, 8)?;
    if !(1..=12).contains(&month) || day < 1 || day > days_in(year, month) {
        return None;
    }
    let mut out = Fields {
        year,
        month,
        day,
        hour: 0,
        minute: 0,
        second: 0,
        microsecond: 0,
        offset_seconds: None,
    };
    if b.len() == 10 {
        return Some(out);
    }
    if !matches!(b[10], b'T' | b't' | b' ' | b'_') {
        return None;
    }
    let hour = digits2(b, 11)?;
    if b.get(13) != Some(&b':') {
        return None;
    }
    let minute = digits2(b, 14)?;
    if hour > 23 || minute > 59 {
        return None;
    }
    out.hour = hour;
    out.minute = minute;
    let mut pos = 16;
    if b.get(pos) == Some(&b':') {
        let second = digits2(b, pos + 1)?;
        if second > 59 {
            return None;
        }
        out.second = second;
        pos += 3;
        if matches!(b.get(pos), Some(b'.' | b',')) {
            pos += 1;
            let start = pos;
            let mut micro = 0u32;
            while let Some(&d) = b.get(pos) {
                if !d.is_ascii_digit() {
                    break;
                }
                if pos - start < 6 {
                    micro = micro * 10 + u32::from(d - b'0');
                }
                pos += 1;
            }
            let n = pos - start;
            if n == 0 {
                return None;
            }
            for _ in n..6 {
                micro *= 10;
            }
            out.microsecond = micro;
        }
    }
    match b.get(pos) {
        None => {}
        Some(b'Z' | b'z') => {
            out.offset_seconds = Some(0);
            pos += 1;
        }
        Some(&sign @ (b'+' | b'-')) => {
            let h = digits2(b, pos + 1)?;
            pos += 3;
            if b.get(pos) == Some(&b':') {
                pos += 1;
            }
            let m = digits2(b, pos)?;
            pos += 2;
            if h > 23 || m > 59 {
                return None;
            }
            let total = i64::from(h * 3600 + m * 60);
            out.offset_seconds = Some(if sign == b'-' { -total } else { total });
        }
        Some(_) => return None,
    }
    (pos == b.len()).then_some(out)
}

const MS_WATERSHED: i64 = 20_000_000_000;
const MIN_SECOND: i64 = -62_167_219_200;
const MAX_SECOND: i64 = 253_402_300_799;

/// speedate's `DateTime::from_timestamp_with_config`: a timestamp past
/// 2e10 in magnitude is milliseconds; the result is UTC.
fn from_timestamp(timestamp: i64, microsecond: u32) -> Option<Fields> {
    let magnitude = timestamp.checked_abs()?;
    let (mut second, extra) = if magnitude <= MS_WATERSHED {
        (timestamp, 0u32)
    } else {
        let mut s = timestamp / 1000;
        let mut us = (timestamp % 1000) * 1000;
        if us < 0 {
            s -= 1;
            us += 1_000_000;
        }
        (s, us as u32)
    };
    let mut total = microsecond.checked_add(extra)?;
    if total >= 1_000_000 {
        second = second.checked_add(i64::from(total / 1_000_000))?;
        total %= 1_000_000;
    }
    if !(MIN_SECOND..=MAX_SECOND).contains(&second) {
        return None;
    }
    let days = second.div_euclid(86_400);
    let of_day = second.rem_euclid(86_400) as u32;
    let (year, month, day) = civil_from_days(days);
    Some(Fields {
        year,
        month,
        day,
        hour: of_day / 3600,
        minute: of_day / 60 % 60,
        second: of_day % 60,
        microsecond: total,
        offset_seconds: Some(0),
    })
}

/// The proleptic Gregorian date `days` after 1970-01-01.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

/// speedate's integer reading: an optional sign and ASCII digits, in i64.
fn int_bytes(b: &[u8]) -> Option<i64> {
    let (negative, digits) = match b.first() {
        Some(b'-') => (true, &b[1..]),
        Some(b'+') => (false, &b[1..]),
        _ => (false, b),
    };
    if digits.is_empty() {
        return None;
    }
    let mut value: i64 = 0;
    for &d in digits {
        if !d.is_ascii_digit() {
            return None;
        }
        let d = i64::from(d - b'0');
        value = value.checked_mul(10)?;
        value = if negative {
            value.checked_sub(d)?
        } else {
            value.checked_add(d)?
        };
    }
    Some(value)
}

/// speedate's `DateTime::parse_bytes_with_config`: RFC 3339, else a
/// unix timestamp written as an integer or (with a `.`) a float.
fn speedate(b: &[u8]) -> Option<Fields> {
    if let Some(f) = rfc3339(b) {
        return Some(f);
    }
    if let Some(i) = int_bytes(b) {
        return from_timestamp(i, 0);
    }
    if !b.contains(&b'.') {
        return None;
    }
    let f: f64 = std::str::from_utf8(b).ok()?.parse().ok()?;
    if !f.is_finite() {
        return None;
    }
    let f = if f.abs() > MS_WATERSHED as f64 {
        f / 1000.0
    } else {
        f
    };
    let floor = f.floor();
    let micro = ((f - floor) * 1_000_000.0).round() as u32;
    from_timestamp(floor as i64, micro)
}

/// pydantic-core's `float_as_datetime`.
fn float_timestamp(f: f64) -> Option<Fields> {
    if f.is_nan() {
        return None;
    }
    let mut micro = f.fract().abs() * 1_000_000.0;
    if f.abs() > MS_WATERSHED as f64 {
        micro /= 1000.0;
    }
    from_timestamp(f.floor() as i64, micro.round() as u32)
}

/// Python's `datetime` from speedate's fields; None for year 0.
fn build(f: Fields) -> Option<DateTime> {
    DateTime::new(
        f.year,
        f.month,
        f.day,
        f.hour,
        f.minute,
        f.second,
        f.microsecond,
        f.offset_seconds.map(|s| s * 1_000_000),
    )
    .ok()
}

/// 2**1024 - 2**970: the least magnitude CPython's int-to-float conversion
/// overflows at (it rounds half to even, and the largest double's
/// significand is odd, so the halfway point rounds up to 2**1024).
const FLOAT_OVERFLOW: &str = "179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497792";

/// Does `float(int(text))` raise OverflowError? `text` is canonical
/// decimal (no leading zeros, an optional `-`).
fn int_overflows_float(text: &str) -> bool {
    let digits = text.trim_start_matches('-');
    digits.len() > FLOAT_OVERFLOW.len()
        || (digits.len() == FLOAT_OVERFLOW.len() && digits >= FLOAT_OVERFLOW)
}

/// A `datetime` field.
pub fn datetime(arg: Arg<'_>) -> Result<DateTime, Kind> {
    let fields = match arg {
        Arg::Py(PyValue::DateTime(dt)) => return Ok(*dt),
        Arg::Py(PyValue::Str(s)) => match utf8_of(s) {
            Some(text) => speedate(text.as_bytes()).ok_or("datetime_from_date_parsing")?,
            None => return Err("string_unicode"),
        },
        Arg::Bytes(b) => speedate(b).ok_or("datetime_from_date_parsing")?,
        Arg::Py(PyValue::Int(t)) => match t.parse::<i64>() {
            Ok(i) => from_timestamp(i, 0).ok_or("datetime_parsing")?,
            // Past i64 pydantic-core reads the int as a float: where that
            // conversion overflows, the input is not a number to it.
            Err(_) if int_overflows_float(t) => return Err("datetime_type"),
            Err(_) => return Err("datetime_parsing"),
        },
        Arg::Py(PyValue::Float(f)) => float_timestamp(*f).ok_or("datetime_parsing")?,
        Arg::Py(_) => return Err("datetime_type"),
    };
    build(fields).ok_or("datetime_parsing")
}

/// `datetime | None`.
pub fn opt_datetime(arg: Arg<'_>) -> Result<Option<DateTime>, Kind> {
    match arg {
        Arg::Py(PyValue::None) => Ok(None),
        other => datetime(other).map(Some),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_dates() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(-719_528), (0, 1, 1));
        assert_eq!(civil_from_days(2_932_896), (9999, 12, 31));
    }
}
