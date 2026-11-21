//! Cases a plausible wrong XID implementation fails.

use bettermemory_core::unicode::xid::{is_identifier, is_xid_continue, is_xid_start};

fn s(text: &str) -> Vec<u32> {
    text.chars().map(u32::from).collect()
}

#[test]
fn underscore_starts_an_identifier_but_is_not_xid_start() {
    assert!(!is_xid_start(0x5F));
    assert!(is_xid_continue(0x5F));
    assert!(is_identifier(&s("_")));
    assert!(is_identifier(&s("__a1")));
    // U+FF3F FULLWIDTH LOW LINE is Pc: continue only, never first.
    assert!(!is_identifier(&[0xFF3F]));
    assert!(is_identifier(&[0x61, 0xFF3F]));
}

#[test]
fn digits_continue_but_do_not_start() {
    for d in b'0'..=b'9' {
        assert!(!is_xid_start(u32::from(d)));
        assert!(is_xid_continue(u32::from(d)));
    }
    assert!(!is_identifier(&s("1a")));
    assert!(is_identifier(&s("a1")));
    // ARABIC-INDIC DIGIT ZERO (Nd) and ETHIOPIC DIGIT ONE (Other_ID_Continue).
    for cp in [0x0660, 0x1369] {
        assert!(!is_xid_start(cp) && is_xid_continue(cp), "{cp:X}");
    }
}

#[test]
fn other_id_start_members_start() {
    // SCRIPT CAPITAL P, ESTIMATED SYMBOL and the two MONGOLIAN LETTER ALI
    // GALI baluda marks: Other_ID_Start, not letters.
    for cp in [0x2118, 0x212E, 0x1885, 0x1886] {
        assert!(is_xid_start(cp), "{cp:X}");
        assert!(is_identifier(&[cp]), "{cp:X}");
    }
    // The KATAKANA-HIRAGANA sound marks are Other_ID_Start too, but NFKC
    // turns them into a space and a mark: neither XID_Start nor continue.
    for cp in [0x309B, 0x309C] {
        assert!(!is_xid_start(cp) && !is_xid_continue(cp), "{cp:X}");
    }
}

#[test]
fn middle_dots_continue_only() {
    // MIDDLE DOT, GREEK ANO TELEIA: Other_ID_Continue.
    for cp in [0x00B7, 0x0387] {
        assert!(!is_xid_start(cp), "{cp:X}");
        assert!(is_xid_continue(cp), "{cp:X}");
        assert!(!is_identifier(&[cp]));
        assert!(is_identifier(&[0x61, cp]));
    }
}

#[test]
fn xid_excludes_what_nfkc_would_change() {
    // GREEK YPOGEGRAMMENI is ID_Start but not XID_Start; no NFKC is applied.
    assert!(!is_xid_start(0x037A));
    assert!(!is_identifier(&[0x037A]));
    // THAI CHARACTER SARA AM: ID_Continue, not XID_Start.
    assert!(!is_xid_start(0x0E33));
    // The ligature fi is XID_Start as itself.
    assert!(is_identifier(&[0xFB01]));
    // ZWJ and ZWNJ continue; a variation selector continues.
    for cp in [0x200C, 0x200D, 0xE0100] {
        assert!(is_xid_continue(cp) && !is_xid_start(cp), "{cp:X}");
    }
}

#[test]
fn bounds_and_surrogates() {
    assert!(!is_identifier(&[]));
    for cp in [0xD800, 0xDFFF, 0x10FFFF, 0x110000, u32::MAX, 0, 0x7F, 0x80] {
        assert!(!is_xid_start(cp) && !is_xid_continue(cp), "{cp:X}");
    }
    assert!(is_xid_start(0x20000) && is_xid_start(0x1D400));
    assert!(!is_identifier(&s("a b")) && !is_identifier(&s("a-b")));
}
