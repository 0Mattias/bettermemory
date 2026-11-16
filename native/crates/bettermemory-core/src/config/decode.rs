//! `bytes.decode("utf-8-sig")` as CPython 3.13 decodes a config file: a
//! leading byte-order mark dropped, and the `UnicodeDecodeError` text of
//! the first bad sequence (its position counted after the mark, since the
//! codec decodes the rest as a slice).

const BOM: &[u8] = b"\xef\xbb\xbf";

fn is_continuation(b: u8) -> bool {
    (0x80..=0xBF).contains(&b)
}

/// The first error's (start, end, reason) as CPython's UTF-8 decoder
/// reports it (`Objects/stringlib/codecs.h`, `utf8_decode`).
fn first_error(data: &[u8]) -> Option<(usize, usize, &'static str)> {
    let mut s = 0;
    let end = data.len();
    while s < end {
        let ch = data[s];
        if ch < 0x80 {
            s += 1;
            continue;
        }
        let left = end - s;
        let at = |i: usize| data[s + i];
        // The width of the sequence decoded, or an error kind: 0 is
        // "unexpected end of data", 1 "invalid start byte", 2..=4 an
        // invalid continuation byte after (kind - 1) good bytes.
        let outcome: Result<usize, usize> = if ch < 0xC2 {
            Err(1)
        } else if ch < 0xE0 {
            if left < 2 {
                Err(0)
            } else if !is_continuation(at(1)) {
                Err(2)
            } else {
                Ok(2)
            }
        } else if ch < 0xF0 {
            let bad2 =
                |c2: u8| !is_continuation(c2) || (if c2 < 0xA0 { ch == 0xE0 } else { ch == 0xED });
            if left < 3 {
                if left < 2 {
                    Err(0)
                } else if bad2(at(1)) {
                    Err(2)
                } else {
                    Err(0)
                }
            } else if bad2(at(1)) {
                Err(2)
            } else if !is_continuation(at(2)) {
                Err(3)
            } else {
                Ok(3)
            }
        } else if ch < 0xF5 {
            let bad2 =
                |c2: u8| !is_continuation(c2) || (if c2 < 0x90 { ch == 0xF0 } else { ch == 0xF4 });
            if left < 4 {
                if left < 2 {
                    Err(0)
                } else if bad2(at(1)) {
                    Err(2)
                } else if left < 3 {
                    Err(0)
                } else if !is_continuation(at(2)) {
                    Err(3)
                } else {
                    Err(0)
                }
            } else if bad2(at(1)) {
                Err(2)
            } else if !is_continuation(at(2)) {
                Err(3)
            } else if !is_continuation(at(3)) {
                Err(4)
            } else {
                Ok(4)
            }
        } else {
            Err(1)
        };
        match outcome {
            Ok(width) => s += width,
            Err(0) => return Some((s, end, "unexpected end of data")),
            Err(1) => return Some((s, s + 1, "invalid start byte")),
            Err(kind) => return Some((s, s + kind - 1, "invalid continuation byte")),
        }
    }
    None
}

/// The text, or `str()` of the `UnicodeDecodeError`.
pub fn decode_utf8_sig(raw: &[u8]) -> Result<String, String> {
    let data = raw.strip_prefix(BOM).unwrap_or(raw);
    match first_error(data) {
        None => Ok(std::str::from_utf8(data)
            .expect("no error found, so UTF-8")
            .to_owned()),
        Some((start, end, reason)) => Err(if end == start + 1 {
            format!(
                "'utf-8' codec can't decode byte 0x{:02x} in position {start}: {reason}",
                data[start]
            )
        } else {
            format!(
                "'utf-8' codec can't decode bytes in position {start}-{}: {reason}",
                end - 1
            )
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::decode_utf8_sig;

    #[test]
    fn errors_read_as_cpython_reports_them() {
        let cases: [(&[u8], &str); 12] = [
            (b"\xff", "byte 0xff in position 0: invalid start byte"),
            (
                b"\xef\xbb\xbf\xff",
                "byte 0xff in position 0: invalid start byte",
            ),
            (
                b"ab\xe2\x82",
                "bytes in position 2-3: unexpected end of data",
            ),
            (
                b"ab\xe2Ax",
                "byte 0xe2 in position 2: invalid continuation byte",
            ),
            (
                b"\xe0\x80\x80",
                "byte 0xe0 in position 0: invalid continuation byte",
            ),
            (
                b"\xf0\x90\x80",
                "bytes in position 0-2: unexpected end of data",
            ),
            (
                b"\xf0\x90\x80x",
                "bytes in position 0-2: invalid continuation byte",
            ),
            (
                b"\xed\xa0\x80",
                "byte 0xed in position 0: invalid continuation byte",
            ),
            (b"\xc0\xaf", "byte 0xc0 in position 0: invalid start byte"),
            (b"\xef\xbb", "bytes in position 0-1: unexpected end of data"),
            (
                b"\xf4\x90\x80\x80",
                "byte 0xf4 in position 0: invalid continuation byte",
            ),
            (b"\xf8", "byte 0xf8 in position 0: invalid start byte"),
        ];
        for (raw, want) in cases {
            let got = decode_utf8_sig(raw).unwrap_err();
            assert_eq!(got, format!("'utf-8' codec can't decode {want}"), "{raw:?}");
        }
        assert_eq!(decode_utf8_sig(b"\xef\xbb\xbfa = 1").unwrap(), "a = 1");
    }
}
