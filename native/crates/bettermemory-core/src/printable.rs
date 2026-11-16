//! `_printable` (128 lines at 851c171d): a text made safe to show on one
//! terminal line (`printable`) and as one double-quoted value nothing in
//! it can close (`quoted`). Strings are code points ([`PyStr`]), so a lone
//! surrogate is a character like any other; the categories are the
//! crate's Unicode 15.1.0 tables ([`unicode::category`],
//! [`unicode::is_alnum`]).

use crate::pyvalue::PyStr;
use crate::unicode;

const HEX: &[u8; 16] = b"0123456789abcdef";

fn push_hex(out: &mut PyStr, value: u32, digits: u32) {
    for i in (0..digits).rev() {
        out.push(u32::from(HEX[((value >> (4 * i)) & 0xF) as usize]));
    }
}

fn push_ascii(out: &mut PyStr, text: &str) {
    out.extend(text.bytes().map(u32::from));
}

/// Whether `printable` writes `cp` as its escape: categories Cc, Cf, Cs,
/// Zl and Zp, the tab excepted.
pub fn unprintable(cp: u32) -> bool {
    cp != 0x09 && matches!(unicode::category(cp), "Cc" | "Cf" | "Cs" | "Zl" | "Zp")
}

/// `ch.encode("unicode_escape").decode("ascii")` for one code point.
fn unicode_escape(out: &mut PyStr, cp: u32) {
    if cp >= 0x10000 {
        push_ascii(out, "\\U");
        push_hex(out, cp, 8);
    } else if cp >= 0x100 {
        push_ascii(out, "\\u");
        push_hex(out, cp, 4);
    } else if cp == u32::from(b'\\') {
        push_ascii(out, "\\\\");
    } else if cp == 0x09 {
        push_ascii(out, "\\t");
    } else if cp == 0x0A {
        push_ascii(out, "\\n");
    } else if cp == 0x0D {
        push_ascii(out, "\\r");
    } else if !(0x20..0x7F).contains(&cp) {
        push_ascii(out, "\\x");
        push_hex(out, cp, 2);
    } else {
        out.push(cp);
    }
}

/// `printable(text)` (`_printable.py` 19-43).
pub fn printable(text: &[u32]) -> PyStr {
    let mut out = Vec::with_capacity(text.len());
    printable_into(text, &mut out);
    out
}

/// `printable`, appended to `out`.
pub fn printable_into(text: &[u32], out: &mut PyStr) {
    for &cp in text {
        if unprintable(cp) {
            unicode_escape(out, cp);
        } else {
            out.push(cp);
        }
    }
}

/// `_QUOTATION_MARKS | _QUOTE_CONFUSABLES` (`_QUOTE_LIKE`, 51-78).
pub const QUOTE_LIKE: &[u32] = &[
    0x00AB, 0x00BB, 0x02BA, 0x02DD, 0x02EE, 0x02F6, 0x05F2, 0x05F4, 0x1CD3, 0x2018, 0x2019, 0x201A,
    0x201B, 0x201C, 0x201D, 0x201E, 0x201F, 0x2033, 0x2036, 0x2039, 0x203A, 0x2E42, 0x3003, 0x300C,
    0x300D, 0x300E, 0x300F, 0x301D, 0x301E, 0x301F, 0xFE41, 0xFE42, 0xFE43, 0xFE44, 0xFF02, 0xFF07,
    0xFF62, 0xFF63,
];

/// `_QUOTE_COMBINING` (89).
pub const QUOTE_COMBINING: [u32; 2] = [0x030B, 0x030E];

/// `_quote_like(ch, base)` (92-97); `base` None is the reference's "".
fn quote_like(cp: u32, base: Option<u32>) -> bool {
    if QUOTE_COMBINING.contains(&cp) {
        return !base.is_some_and(unicode::is_alnum);
    }
    QUOTE_LIKE.binary_search(&cp).is_ok() || matches!(unicode::category(cp), "Pi" | "Pf")
}

/// `json.dumps(text, ensure_ascii=False)` of a `str`.
fn dumps_unicode(text: &[u32], out: &mut PyStr) {
    out.push(u32::from(b'"'));
    for &cp in text {
        match cp {
            0x22 => push_ascii(out, "\\\""),
            0x5C => push_ascii(out, "\\\\"),
            0x08 => push_ascii(out, "\\b"),
            0x0C => push_ascii(out, "\\f"),
            0x0A => push_ascii(out, "\\n"),
            0x0D => push_ascii(out, "\\r"),
            0x09 => push_ascii(out, "\\t"),
            c if c < 0x20 => {
                push_ascii(out, "\\u");
                push_hex(out, c, 4);
            }
            c => out.push(c),
        }
    }
    out.push(u32::from(b'"'));
}

/// `json.dumps(ch)[1:-1]` of a non-ASCII code point (ASCII escaping).
fn ascii_escape(out: &mut PyStr, cp: u32) {
    if cp >= 0x10000 {
        let v = cp - 0x10000;
        push_ascii(out, "\\u");
        push_hex(out, 0xD800 | (v >> 10), 4);
        push_ascii(out, "\\u");
        push_hex(out, 0xDC00 | (v & 0x3FF), 4);
    } else {
        push_ascii(out, "\\u");
        push_hex(out, cp, 4);
    }
}

/// `quoted(text)` (`_printable.py` 100-125).
pub fn quoted(text: &[u32]) -> PyStr {
    let mut out = Vec::with_capacity(text.len() + 2);
    quoted_into(text, &mut out);
    out
}

/// `quoted`, appended to `out`.
pub fn quoted_into(text: &[u32], out: &mut PyStr) {
    let mut dumped = Vec::with_capacity(text.len() + 2);
    dumps_unicode(&printable(text), &mut dumped);
    let mut base: Option<u32> = None;
    for &cp in &dumped {
        // Every quote-like code point is outside ASCII, so its JSON
        // escape is `\uXXXX` (two for an astral one).
        let last = if quote_like(cp, base) {
            ascii_escape(out, cp);
            *out.last().expect("an escape was written")
        } else {
            out.push(cp);
            cp
        };
        if !unicode::category(last).starts_with('M') {
            base = Some(last);
        }
    }
}
