//! Inputs a plausible wrong implementation of `credentials::gate` answers
//! otherwise: `str.isupper` read as `Lu` alone (Roman numerals and circled
//! letters are uppercase, a titlecase letter is not), `$` read only at the
//! very end, ASCII-only `\d` in the suffix and the datetime shape where
//! `rstrip("0123456789")` is ASCII, separator runs split one at a time, a
//! closer stripped even when its opener is present, a call expression whose
//! `.*` crosses a line feed, the length floor and ceiling off by one, and
//! `str.isdigit` read as `\d`. Each row is the reference's answer
//! (`_is_placeholder`, `_DATETIME_SHAPE_RE.fullmatch`, `_looks_like_secret`,
//! `_NUMBER_SUFFIX_RE.search(...).span()`), read from CPython 3.13.13.

use bettermemory_core::credentials::gate::{
    datetime_shape, is_placeholder, looks_like_secret, number_suffix,
};

type Row = (&'static str, bool, bool, bool, Option<(usize, usize)>);

const CASES: &[Row] = &[
    ("Abcdefgh1234.", false, false, true, None),
    ("Abcdefgh1234.!?", false, false, true, None),
    ("\u{1c5}bcdefg-1234", false, false, false, Some((7, 12))),
    ("\u{2160}bcdefg-1234", false, false, true, Some((7, 12))),
    ("\u{24b6}bcdefg-1234", false, false, true, Some((7, 12))),
    ("changeme1\u{a}", false, false, false, Some((8, 9))),
    ("changeme-\u{661}\u{662}", true, false, false, Some((8, 11))),
    (
        "dummy_password_\u{661}",
        false,
        false,
        false,
        Some((14, 16)),
    ),
    ("dummy__password--123", true, false, false, Some((16, 20))),
    ("your_api_key_9", true, false, false, Some((12, 14))),
    ("2026-01-15T00:30:00Z", false, true, false, None),
    (
        "\u{ff12}\u{ff10}\u{ff12}\u{ff16}-01-15T00:30",
        false,
        true,
        false,
        Some((14, 16)),
    ),
    ("2026-01-15T00:30:00.Z", false, false, true, None),
    ("20260115T0030001Z", false, false, true, None),
    ("20260115T003000+0200", false, true, false, Some((16, 20))),
    ("foo.bar(x\u{a}y)", false, false, false, None),
    ("foo.bar(xyzw12)", false, false, false, None),
    ("foo.bar(", false, false, false, None),
    ("{Abcdefgh1234}", false, false, false, None),
    ("Abcdefgh1234}", false, false, true, None),
    ("Abcdefgh1234%", false, false, true, None),
    ("Abcd%efgh1234%", false, false, false, None),
    ("a:/Bcdefgh1234X", false, false, false, None),
    ("ab/Cdefgh1234X", false, false, true, None),
    ("abc/def1234/xyz", false, false, false, None),
    ("ABC_DEF_123_XYZ", false, false, false, None),
    ("ABC__DEF123XYZ", false, false, true, None),
    ("Abcdefg\u{e9}1234", false, false, true, Some((8, 12))),
    ("\u{130}bcdefgh1234", false, false, true, Some((8, 12))),
    ("  'Abcdefgh1234'  ", false, false, true, None),
    ("Abcabcabc1231", false, false, false, Some((9, 13))),
    ("Abcdefgh1234\\", false, false, false, None),
    (
        "Abcdefg\u{661}\u{662}\u{663}\u{664}\u{665}",
        false,
        false,
        true,
        Some((7, 12)),
    ),
    ("Abcdefg\u{b2}\u{b3}\u{b9}xyz", false, false, true, None),
    ("Foo.bar(x\u{a}Y12)", false, false, true, None),
    ("Foo.bar(xY12)", false, false, false, None),
    ("Foo.bar(xY12)\u{a}", false, false, false, None),
];

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

#[test]
fn every_row_is_the_reference_answer() {
    for &(text, placeholder, shape, secret, suffix) in CASES {
        let t = cps(text);
        assert_eq!(is_placeholder(&t), placeholder, "placeholder {text:?}");
        assert_eq!(datetime_shape(&t), shape, "shape {text:?}");
        assert_eq!(looks_like_secret(&t), secret, "secret {text:?}");
        assert_eq!(number_suffix(&t), suffix, "suffix {text:?}");
    }
    assert!(!CASES.is_empty());
}

#[test]
fn the_length_bounds() {
    // 1,024 code points pass the ceiling, 1,026 (stripped of nothing) do
    // not; 11 fail the floor and 12 pass it.
    let mut at_ceiling = cps("A1");
    at_ceiling.extend(cps(&"bcdefgh".repeat(146)));
    assert_eq!(at_ceiling.len(), 1024);
    assert!(looks_like_secret(&at_ceiling));
    let mut over = cps(&"x".repeat(1024));
    over.extend(cps("A1"));
    assert!(!looks_like_secret(&over));
    assert!(!looks_like_secret(&cps("Abcdefgh123")));
    assert!(looks_like_secret(&cps("Abcdefgh1234")));
}
