//! Inputs a plausible wrong reading of `re`'s case folding answers otherwise
//! than CPython 3.13.13 does: each assertion is the interpreter's answer
//! (`_sre.unicode_tolower`, `_sre.unicode_iscased`, the first code point of
//! `str.upper`, `re._casefix._EXTRA_CASES`, `re.fullmatch` under
//! IGNORECASE), and the test's name or comment says which reading it kills.

use bettermemory_core::pyre::fold::{
    extra_cases, sre_iscased, sre_lower, sre_upper, Lit, Unreproduced, ASCII, IGNORECASE, LOCALE,
    UNICODE,
};
use bettermemory_core::unicode;

const N: u32 = 0x110000;

/// Every code point `lit` matches, ascending.
fn set(lit: Lit) -> Vec<u32> {
    (0..N).filter(|&c| lit.matches(c)).collect()
}

/// The literal `c` under IGNORECASE on a `str` pattern.
fn fold(c: u32) -> Lit {
    Lit::new(c, IGNORECASE | UNICODE).expect("the Unicode fold")
}

fn folded(c: u32) -> Vec<u32> {
    set(fold(c))
}

#[test]
fn lower_is_one_code_point_out_with_no_context() {
    // `str.lower` gives U+0069 U+0307; a table of full mappings compared
    // whole finds no match for `i`.
    assert_eq!(sre_lower(0x130), 0x69);
    assert_eq!(unicode::lower(&[0x130]), [0x69, 0x307]);
    // `str.lower` reads final sigma from the text around; `re` reads one
    // code point.
    assert_eq!(sre_lower(0x3A3), 0x3C3);
    assert_eq!(unicode::lower(&[0x41, 0x3A3]), [0x61, 0x3C2]);
    // Lower, not casefold: Cherokee folds to its capitals, U+00DF to `ss`,
    // U+03C2 to U+03C3, U+017F to `s`.
    assert_eq!(sre_lower(0x13A0), 0xAB70);
    assert_eq!(sre_lower(0xAB70), 0xAB70);
    assert_eq!(sre_lower(0xDF), 0xDF);
    assert_eq!(sre_lower(0x3C2), 0x3C2);
    assert_eq!(sre_lower(0x17F), 0x17F);
    assert_eq!(sre_lower(0x131), 0x131);
    // Compatibility letters lower to the plain letter.
    assert_eq!(sre_lower(0x212A), 0x6B);
    assert_eq!(sre_lower(0x212B), 0xE5);
    assert_eq!(sre_lower(0x2126), 0x3C9);
    assert_eq!(sre_lower(0x1E9E), 0xDF);
    assert_eq!(sre_lower(0x3F4), 0x3B8);
    // Title case lowers as upper case does.
    assert_eq!(sre_lower(0x1C5), 0x1C6);
    assert_eq!(sre_lower(0x1C4), 0x1C6);
    assert_eq!(sre_lower(0x1F88), 0x1F80);
}

#[test]
fn lower_edges_of_the_tables() {
    // ASCII: only A to Z move, by 0x20.
    for c in 0..0x80u32 {
        let want = if (0x41..=0x5A).contains(&c) {
            c + 0x20
        } else {
            c
        };
        assert_eq!(sre_lower(c), want, "{c:#x}");
    }
    // Latin-1: U+00D7 stands between the letters; U+00DE is the last.
    assert_eq!(sre_lower(0xC0), 0xE0);
    assert_eq!(sre_lower(0xD7), 0xD7);
    assert_eq!(sre_lower(0xDE), 0xFE);
    assert_eq!(sre_lower(0x178), 0xFF);
    // Past the BMP (a table of 16-bit code points loses these).
    assert_eq!(sre_lower(0x10400), 0x10428);
    assert_eq!(sre_lower(0x118A0), 0x118C0);
    assert_eq!(sre_lower(0x16E40), 0x16E60);
    // The last code point with a lower case, and the first without.
    assert_eq!(sre_lower(0x1E921), 0x1E943);
    assert_eq!(sre_lower(0x1E922), 0x1E922);
    assert_eq!(sre_lower(0x1C90), 0x10D0);
    assert_eq!(sre_lower(0x2C2F), 0x2C5F);
    assert_eq!(sre_lower(0xA7C0), 0xA7C1);
    // Counts over every code point: 1,433 change.
    assert_eq!((0..N).filter(|&c| sre_lower(c) != c).count(), 1433);
}

#[test]
fn upper_is_the_first_code_point_of_the_full_mapping() {
    // A simple (one to one) table has no upper case for these.
    assert_eq!(sre_upper(0xDF), 0x53);
    assert_eq!(sre_upper(0x149), 0x2BC);
    assert_eq!(sre_upper(0x1F0), 0x4A);
    assert_eq!(sre_upper(0x390), 0x399);
    assert_eq!(sre_upper(0x587), 0x535);
    assert_eq!(sre_upper(0xFB05), 0x53);
    assert_eq!(sre_upper(0xFB06), 0x53);
    // The full mapping's first code point, not the simple mapping (U+1F88).
    assert_eq!(sre_upper(0x1F80), 0x1F08);
    // One to one.
    assert_eq!(sre_upper(0x131), 0x49);
    assert_eq!(sre_upper(0x17F), 0x53);
    assert_eq!(sre_upper(0xB5), 0x39C);
    assert_eq!(sre_upper(0x345), 0x399);
    assert_eq!(sre_upper(0x1FBE), 0x399);
    assert_eq!(sre_upper(0x3C2), 0x3A3);
    assert_eq!(sre_upper(0xFF), 0x178);
    assert_eq!(sre_upper(0x10D0), 0x1C90);
    assert_eq!(sre_upper(0xAB70), 0x13A0);
    assert_eq!(sre_upper(0x1C5), 0x1C4);
    assert_eq!(sre_upper(0x1C6), 0x1C4);
    assert_eq!(sre_upper(0x10428), 0x10400);
    assert_eq!(sre_upper(0x1E943), 0x1E921);
    // No upper case.
    assert_eq!(sre_upper(0x130), 0x130);
    assert_eq!(sre_upper(0x1E9E), 0x1E9E);
    assert_eq!(sre_upper(0x2B0), 0x2B0);
    assert_eq!(sre_upper(0xF7), 0xF7);
    for c in 0..0x80u32 {
        let want = if (0x61..=0x7A).contains(&c) {
            c - 0x20
        } else {
            c
        };
        assert_eq!(sre_upper(c), want, "{c:#x}");
    }
    assert_eq!((0..N).filter(|&c| sre_upper(c) != c).count(), 1525);
}

#[test]
fn cased_is_not_the_unicode_property() {
    // Lowercase letters and modifiers with no case pair: the property
    // Cased, and `str.islower`, say yes; `_sre` says no.
    for c in [0xAA, 0xBA, 0x138, 0x1D00, 0x2B0, 0x2071, 0x1D2C] {
        assert!(!sre_iscased(c), "{c:#x}");
    }
    assert!(unicode::is_cased(0xAA) && unicode::is_cased(0x138));
    // Cased by the lower case, by the upper case only, by a title case, by
    // an upper case of two code points, a mark, numerals, circled letters.
    for c in [
        0x41, 0x61, 0xB5, 0xDF, 0x130, 0x131, 0x149, 0x17F, 0x1C5, 0x345, 0x3C2, 0x587, 0x10D0,
        0x1E9E, 0x1F88, 0x1FBE, 0x2160, 0x2170, 0x24B6, 0x24D0, 0x10400, 0x1E943,
    ] {
        assert!(sre_iscased(c), "{c:#x}");
    }
    // Letters without case, digits, signs, surrogates, the edges.
    for c in [
        0x30, 0x5F, 0xD7, 0xF7, 0x1BB, 0x294, 0xD800, 0xDFFF, 0x1E944, 0x10FFFF,
    ] {
        assert!(!sre_iscased(c), "{c:#x}");
    }
    assert_eq!((0..N).filter(|&c| sre_iscased(c)).count(), 2927);
    assert_eq!((0x10000..N).filter(|&c| sre_iscased(c)).count(), 520);
    // The crate's `unicode::is_cased` (what `str.lower` asks for a final
    // sigma) is another set: 1,334 code points apart, the first U+00AA.
    let apart: Vec<u32> = (0..N)
        .filter(|&c| unicode::is_cased(c) != sre_iscased(c))
        .collect();
    assert_eq!((apart.len(), apart[0]), (1334, 0xAA));
}

#[test]
fn extra_cases_are_the_table_of_casefix() {
    assert_eq!(extra_cases(0x69), [0x131]);
    assert_eq!(extra_cases(0x131), [0x69]);
    assert_eq!(extra_cases(0x73), [0x17F]);
    assert_eq!(extra_cases(0xB5), [0x3BC]);
    assert_eq!(extra_cases(0x3C3), [0x3C2]);
    // Three letters that are one: each key names the other two, in the
    // interpreter's order.
    assert_eq!(extra_cases(0x345), [0x3B9, 0x1FBE]);
    assert_eq!(extra_cases(0x3B9), [0x345, 0x1FBE]);
    assert_eq!(extra_cases(0x1FBE), [0x345, 0x3B9]);
    assert_eq!(extra_cases(0x442), [0x1C84, 0x1C85]);
    assert_eq!(extra_cases(0xFB05), [0xFB06]);
    assert_eq!(extra_cases(0xFB06), [0xFB05]);
    // Keys are lower cases: an upper case, `k`, U+212A and U+00DF have none.
    for c in [
        0x49,
        0x53,
        0x6B,
        0x212A,
        0xDF,
        0x1E9E,
        0x130,
        0,
        0xFB07,
        N,
        u32::MAX,
    ] {
        assert_eq!(extra_cases(c), [0u32; 0], "{c:#x}");
    }
    let keys: Vec<u32> = (0..N).filter(|&c| !extra_cases(c).is_empty()).collect();
    assert_eq!(keys.len(), 50);
    assert_eq!(
        keys.iter().map(|&k| extra_cases(k).len()).sum::<usize>(),
        56
    );
    assert_eq!(keys.last(), Some(&0xFB06));
}

#[test]
fn the_literal_is_lowered_then_joined_by_its_extra_cases() {
    assert_eq!(
        fold(0x4B),
        Lit::Fold {
            lower: 0x6B,
            extra: &[]
        }
    );
    assert_eq!(
        fold(0x49),
        Lit::Fold {
            lower: 0x69,
            extra: &[0x131]
        }
    );
    assert_eq!(
        fold(0x130),
        Lit::Fold {
            lower: 0x69,
            extra: &[0x131]
        }
    );
    assert_eq!(
        fold(0x131),
        Lit::Fold {
            lower: 0x131,
            extra: &[0x69]
        }
    );
    // Without the flag, and for an uncased code point under it, exact.
    assert_eq!(Lit::new(0x4B, UNICODE), Ok(Lit::Exact(0x4B)));
    assert_eq!(fold(0xAA), Lit::Exact(0xAA));
    assert_eq!(fold(0x31), Lit::Exact(0x31));
    assert_eq!(fold(0xD800), Lit::Exact(0xD800));
}

#[test]
fn the_folds_by_other_tables_are_refused_not_answered() {
    // `int(re.I)`, `int(re.L)`, `int(re.U)`, `int(re.A)`.
    assert_eq!((IGNORECASE, LOCALE, UNICODE, ASCII), (2, 4, 32, 256));
    // Under `re.ASCII` only A to Z fold: `re.fullmatch('k', '\u212a',
    // re.I | re.A)` and `re.fullmatch('\u00e9', '\u00c9', re.I | re.A)` are
    // None, where the Unicode fold matches. `re.compile('k', re.I |
    // re.A).flags` is 258: no UNICODE.
    assert!(fold(0x6B).matches(0x212A) && fold(0xE9).matches(0xC9));
    assert_eq!(Lit::new(0x6B, 258), Err(Unreproduced::AsciiFold));
    assert_eq!(
        Lit::new(0xE9, IGNORECASE | ASCII),
        Err(Unreproduced::AsciiFold)
    );
    // A bytes pattern never carries UNICODE: `re.compile(b'k', re.I).flags`
    // is 2, and `re.fullmatch(b'\xe9', b'\xc9', re.I)` is None.
    assert_eq!(Lit::new(0xE9, IGNORECASE), Err(Unreproduced::AsciiFold));
    // LOCALE, on a bytes pattern (`re.compile(b'k', re.I | re.L).flags` is
    // 6), folds by the C library's locale. `_compile` reads it before
    // UNICODE.
    assert_eq!(Lit::new(0x6B, 6), Err(Unreproduced::Locale));
    assert_eq!(
        Lit::new(0x6B, IGNORECASE | LOCALE | UNICODE),
        Err(Unreproduced::Locale)
    );
    // The mode is refused whatever the literal, an uncased one too.
    for c in [0, 0x31, 0x41, 0xAA, 0xD800, 0x10FFFF, 0x110000, u32::MAX] {
        assert_eq!(Lit::new(c, IGNORECASE), Err(Unreproduced::AsciiFold));
        assert_eq!(Lit::new(c, IGNORECASE | LOCALE), Err(Unreproduced::Locale));
    }
    // Without IGNORECASE no mode folds.
    for flags in [0, UNICODE, ASCII, LOCALE, LOCALE | ASCII] {
        assert_eq!(Lit::new(0x6B, flags), Ok(Lit::Exact(0x6B)), "{flags}");
    }
    // MULTILINE, DOTALL, VERBOSE and DEBUG change no literal.
    for other in [8, 16, 64, 128] {
        assert_eq!(Lit::new(0x4B, IGNORECASE | UNICODE | other), Ok(fold(0x4B)));
        assert_eq!(Lit::new(0x4B, UNICODE | other), Ok(Lit::Exact(0x4B)));
    }
}

#[test]
fn ascii_letters_match_past_ascii() {
    // An ASCII-only fold, or simple case folding, loses the last of each.
    assert_eq!(folded(0x6B), [0x4B, 0x6B, 0x212A]);
    assert_eq!(folded(0x73), [0x53, 0x73, 0x17F]);
    assert_eq!(folded(0x69), [0x49, 0x69, 0x130, 0x131]);
    // The pattern's own case does not matter, nor which of the set it is.
    assert_eq!(folded(0x4B), folded(0x6B));
    assert_eq!(folded(0x212A), folded(0x6B));
    assert_eq!(folded(0x130), folded(0x69));
    assert_eq!(folded(0x131), folded(0x69));
    assert_eq!(folded(0x17F), folded(0x73));
    // Every other ASCII letter matches its two cases and nothing else.
    for c in (0x41..=0x5Au32).filter(|c| ![0x49, 0x4B, 0x53].contains(c)) {
        assert_eq!(folded(c), [c, c + 0x20], "{c:#x}");
        assert_eq!(folded(c + 0x20), [c, c + 0x20], "{c:#x}");
    }
    // Not a fold by flipping bit 5: these are uncased.
    for (a, b) in [(0x40, 0x60), (0x5B, 0x7B), (0x5F, 0x7F), (0x31, 0x11)] {
        assert_eq!(folded(a), [a]);
        assert_eq!(folded(b), [b]);
    }
}

#[test]
fn no_fold_takes_two_characters_and_none_goes_by_upper_case() {
    // casefold reads U+00DF as `ss`; a fold by upper case matches it to `S`.
    assert_eq!(folded(0xDF), [0xDF, 0x1E9E]);
    assert_eq!(folded(0x1E9E), [0xDF, 0x1E9E]);
    // Upper cases U+02BC, `J` and U+1F08 are no match: only the lower
    // case is compared.
    assert_eq!(folded(0x149), [0x149]);
    assert_eq!(folded(0x1F0), [0x1F0]);
    assert_eq!(folded(0x1F80), [0x1F80, 0x1F88]);
    assert_eq!(folded(0x587), [0x587]);
    // The two ligatures match each other and not `st`'s letters.
    assert_eq!(folded(0xFB05), [0xFB05, 0xFB06]);
}

#[test]
fn greek_letters_and_their_symbols() {
    // Sigma: the capital, the final and the plain one, whichever is written.
    for c in [0x3A3, 0x3C2, 0x3C3] {
        assert_eq!(folded(c), [0x3A3, 0x3C2, 0x3C3], "{c:#x}");
    }
    assert_eq!(folded(0xB5), [0xB5, 0x39C, 0x3BC]);
    assert_eq!(folded(0x3BC), [0xB5, 0x39C, 0x3BC]);
    assert_eq!(folded(0x345), [0x345, 0x399, 0x3B9, 0x1FBE]);
    assert_eq!(folded(0x1FBE), [0x345, 0x399, 0x3B9, 0x1FBE]);
    assert_eq!(folded(0x3B8), [0x398, 0x3B8, 0x3D1, 0x3F4]);
    assert_eq!(folded(0x3F4), [0x398, 0x3B8, 0x3D1, 0x3F4]);
    assert_eq!(folded(0x2126), [0x3A9, 0x3C9, 0x2126]);
    assert_eq!(folded(0x390), [0x390, 0x1FD3]);
}

#[test]
fn other_scripts_and_planes() {
    assert_eq!(folded(0x1C5), [0x1C4, 0x1C5, 0x1C6]);
    assert_eq!(folded(0x442), [0x422, 0x442, 0x1C84, 0x1C85]);
    assert_eq!(folded(0xA64B), [0x1C88, 0xA64A, 0xA64B]);
    assert_eq!(folded(0x1E61), [0x1E60, 0x1E61, 0x1E9B]);
    assert_eq!(folded(0x13A0), [0x13A0, 0xAB70]);
    assert_eq!(folded(0x10D0), [0x10D0, 0x1C90]);
    assert_eq!(folded(0x10A0), [0x10A0, 0x2D00]);
    assert_eq!(folded(0x2167), [0x2167, 0x2177]);
    assert_eq!(folded(0xFF), [0xFF, 0x178]);
    assert_eq!(folded(0xE9), [0xC9, 0xE9]);
    assert_eq!(folded(0x10400), [0x10400, 0x10428]);
    assert_eq!(folded(0x10428), [0x10400, 0x10428]);
    assert_eq!(folded(0x1E943), [0x1E921, 0x1E943]);
}

#[test]
fn uncased_code_points_match_only_themselves() {
    for c in [0, 0x30, 0xAA, 0xB2, 0x2B0, 0xD800, 0xDFFF, 0xE000, 0x10FFFF] {
        assert_eq!(folded(c), [c], "{c:#x}");
    }
    // Without the flag nothing folds.
    for c in [0x4B, 0x6B, 0x212A, 0xE9, 0x3A3] {
        assert_eq!(set(Lit::new(c, UNICODE).expect("exact")), [c], "{c:#x}");
    }
}

#[test]
fn folding_is_an_equivalence_over_every_cased_code_point() {
    // What the class rule of a later unit leans on: every member of a
    // folded literal's set is cased and folds to the same set.
    let mut classes = 0;
    for p in (0..N).filter(|&c| sre_iscased(c)) {
        let Lit::Fold { lower, extra } = fold(p) else {
            panic!("{p:#x} is cased");
        };
        assert!(sre_iscased(lower) && sre_lower(lower) == lower, "{p:#x}");
        for &e in extra {
            assert!(sre_iscased(e) && sre_lower(e) == e, "{p:#x}");
            assert_eq!(
                fold(e),
                Lit::Fold {
                    lower: e,
                    extra: extra_cases(e)
                },
                "{p:#x}"
            );
            assert!(extra_cases(e).contains(&lower), "{p:#x}");
        }
        assert!(fold(p).matches(p), "{p:#x}");
        classes += usize::from(lower == p && extra.iter().all(|&e| e > p));
    }
    // 1,468 sets in all: one for each lower case that is the least of its
    // extra cases.
    assert_eq!(classes, 1468);
}
