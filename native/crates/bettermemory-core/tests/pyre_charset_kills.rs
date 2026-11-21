//! Readings of `_optimize_charset` and `_sre`'s class test a plausible
//! port gets wrong, each with the answer of CPython 3.13.13 (checked with
//! `re._compiler._optimize_charset` and `re.fullmatch`).

use bettermemory_core::pyre::charset::{compile_class, optimize_charset, Charset, Fold, InOp, Op};
use bettermemory_core::pyre::tree::{Category, Feature, Member};

const U: u32 = 32;
const I: u32 = 2;
const A: u32 = 256;
const L: u32 = 4;

fn lit(c: char) -> Member {
    Member::Literal(c as u32)
}

fn class(members: &[Member], flags: u32) -> Charset {
    compile_class(members, flags).expect("a class the engine compiles")
}

/// A rewrite that is not shorter and not cased keeps the members as
/// written: `[\U00010000a]` stays in that order, though the map holds `a`.
#[test]
fn not_shorter_keeps_the_members() {
    let m = [Member::Literal(0x10000), lit('a')];
    let (ops, _) = optimize_charset(&m, Fold::None);
    assert_eq!(ops, vec![Op::Literal(0x10000), Op::Literal(0x61)]);
    // Shorter: two literals become one range.
    let (ops, _) = optimize_charset(&[lit('a'), lit('b')], Fold::None);
    assert_eq!(ops, vec![Op::Range(0x61, 0x62)]);
}

/// A literal past the BMP widens the map to 65,536 though it never lands
/// in it, so three runs below 256 become BIGCHARSET, not CHARSET.
#[test]
fn a_member_past_the_bmp_widens_the_map() {
    let (ops, _) = optimize_charset(
        &[lit('a'), lit('c'), lit('e'), Member::Literal(0x10000)],
        Fold::None,
    );
    assert!(matches!(ops[0], Op::BigCharset(_)), "{ops:?}");
    assert_eq!(ops[1], Op::Literal(0x10000));
    let (ops, _) = optimize_charset(&[lit('a'), lit('c'), lit('e')], Fold::None);
    assert!(matches!(ops[0], Op::Charset(_)), "{ops:?}");
}

/// Under the Unicode fold a literal past the BMP goes to the tail lowered;
/// a range past the BMP becomes RANGE_UNI_IGNORE under it and stays a
/// RANGE under the ASCII fold, both with `hascased`.
#[test]
fn past_the_bmp_under_the_folds() {
    let (ops, cased) = optimize_charset(&[Member::Literal(0x10400), lit('x')], Fold::Unicode);
    assert_eq!(ops, vec![Op::Literal(0x78), Op::Literal(0x10428)]);
    assert!(cased);
    let r = Member::Range(0x10000, 0x10001);
    assert_eq!(
        optimize_charset(&[r], Fold::Unicode),
        (vec![Op::RangeUniIgnore(0x10000, 0x10001)], true)
    );
    assert_eq!(
        optimize_charset(&[r], Fold::Ascii),
        (vec![Op::Range(0x10000, 0x10001)], true)
    );
}

/// The engine refuses what it does not match: a member past the BMP under
/// IGNORECASE, the ASCII fold, LOCALE, a category on bytes.
#[test]
fn refusals_by_name() {
    let cat = Member::Category(Category::CATEGORY_DIGIT);
    assert_eq!(
        compile_class(&[Member::Literal(0x10400)], I | U),
        Err(Feature::ClassPastBmpIgnorecase)
    );
    assert_eq!(compile_class(&[lit('A')], I | A), Err(Feature::Ascii));
    assert_eq!(compile_class(&[lit('A')], I), Err(Feature::BytesIgnorecase));
    assert_eq!(compile_class(&[lit('A')], L), Err(Feature::Locale));
    assert_eq!(compile_class(&[cat], 0), Err(Feature::BytesCategory));
    assert_eq!(compile_class(&[cat], A), Err(Feature::Ascii));
    // Past the BMP without IGNORECASE is compiled.
    assert!(class(&[Member::Literal(0x10400)], U).contains(0x10400));
}

/// IN_UNI_IGNORE lowers the text character: `[a-z]` under IGNORECASE holds
/// `K`, KELVIN SIGN and U+0130 (which lower to `k` and `i`), and LONG S and
/// DOTLESS I, which lower to themselves and are marked as the extra cases
/// of `s` and `i`.
#[test]
fn the_fold_lowers_the_text() {
    let set = class(&[Member::Range(0x61, 0x7A)], I | U);
    assert_eq!(set.op, InOp::InUniIgnore);
    for c in [0x4B, 0x212A, 0x17F, 0x130, 0x131] {
        assert!(set.contains(c), "{c:#x}");
    }
    assert!(!set.contains(0x2C));
}

/// A class with no cased member under IGNORECASE keeps its members and
/// compiles to IN.
#[test]
fn uncased_under_ignorecase_is_in() {
    let set = class(&[Member::Range(0x30, 0x39)], I | U);
    assert_eq!(set.op, InOp::In);
    assert_eq!(set.ops, vec![Op::Range(0x30, 0x39)]);
}

/// The Unicode categories, not the ASCII ones.
#[test]
fn categories_are_unicode() {
    let d = class(&[Member::Category(Category::CATEGORY_DIGIT)], U);
    assert!(d.contains(0x663) && !d.contains(0xB2), "decimal, not digit");
    let w = class(&[Member::Category(Category::CATEGORY_WORD)], U);
    assert!(w.contains(0x5F) && w.contains(0xE9) && w.contains(0xB2) && !w.contains(0x2D));
    let s = class(&[Member::Category(Category::CATEGORY_SPACE)], U);
    assert!(s.contains(0x1C) && s.contains(0x85) && !s.contains(0x200B));
}

/// NEGATE turns `ok` where it stands; the parser writes it first only.
#[test]
fn negate_turns_the_answer() {
    let set = class(&[Member::Negate, Member::Range(0x61, 0x7A)], U);
    assert!(!set.contains(0x61) && set.contains(0x41));
    // An empty negated class holds everything, an empty class nothing.
    assert!(class(&[Member::Negate], U).contains(0x10FFFF));
    assert!(!class(&[], U).contains(0));
}

/// BIGCHARSET's chunk numbers index chunks; a code point past the BMP is in
/// no chunk.
#[test]
fn bigcharset_reads_its_chunks() {
    let set = class(&[lit('a'), lit('c'), lit('e'), Member::Literal(0x100)], U);
    assert!(matches!(set.ops[0], Op::BigCharset(_)));
    for (c, want) in [
        (0x61, true),
        (0x62, false),
        (0x100, true),
        (0x101, false),
        (0x10061, false),
    ] {
        assert_eq!(set.contains(c), want, "{c:#x}");
    }
}

/// The extra cases are marked in the map: `[s0]` under the Unicode fold
/// holds U+017F, three runs past Latin-1, so BIGCHARSET; `[k0]` has no
/// extra case (KELVIN SIGN lowers to `k`) and stays two literals.
#[test]
fn extra_cases_are_marked() {
    let (ops, cased) = optimize_charset(&[lit('s'), lit('0')], Fold::Unicode);
    assert!(cased);
    assert!(matches!(ops[..], [Op::BigCharset(_)]), "{ops:?}");
    let (ops, cased) = optimize_charset(&[lit('k'), lit('0')], Fold::Unicode);
    assert!(cased);
    assert_eq!(ops, vec![Op::Literal(0x30), Op::Literal(0x6B)]);
}

/// CHARSET covers all of Latin-1: `[\xe0\xe9\xff]` compiles to CHARSET and
/// `re.fullmatch` matches each member, not `\xe1` or `a`.
#[test]
fn charset_reaches_latin_1() {
    let set = class(
        &[
            Member::Literal(0xe0),
            Member::Literal(0xe9),
            Member::Literal(0xff),
        ],
        U,
    );
    assert!(matches!(set.ops[..], [Op::Charset(_)]), "{:?}", set.ops);
    for (c, want) in [
        (0xe0, true),
        (0xe9, true),
        (0xff, true),
        (0xe1, false),
        (0x61, false),
    ] {
        assert_eq!(set.contains(c), want, "{c:#x}");
    }
}

/// RANGE_UNI_IGNORE also tests the upper case of the lowered character:
/// `re.fullmatch('[\U00010400-\U00010427]', '\U00010400', re.I)` matches,
/// though U+10400 lowers to U+10428, outside the range.
#[test]
fn range_uni_ignore_tests_the_upper_case() {
    let set = Charset {
        op: InOp::InUniIgnore,
        ops: vec![Op::RangeUniIgnore(0x10400, 0x10427)],
    };
    assert!(set.contains(0x10400));
    assert!(set.contains(0x10428));
    assert!(!set.contains(0x10450));
}
