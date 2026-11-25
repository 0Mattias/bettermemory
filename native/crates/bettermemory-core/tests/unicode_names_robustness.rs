//! Hostile inputs for `unicode::names::lookup` and the `\N{...}` escape:
//! very long names, many escapes, every `u32` boundary, and lookup time
//! linear in the count of names.

mod timing;

use bettermemory_core::pysyntax::lex::literal_tokens;
use bettermemory_core::unicode::names::{lookup, stored};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// Every name the table holds, then the CJK unified ideographs U+4E00 to
/// U+9FFF by rule.
fn every_name() -> Vec<(Vec<u32>, u32)> {
    let mut out: Vec<(Vec<u32>, u32)> = stored().map(|(n, cp)| (cps(n), cp)).collect();
    for cp in 0x4E00..=0x9FFF {
        out.push((cps(&format!("CJK UNIFIED IDEOGRAPH-{cp:04X}")), cp));
    }
    out
}

#[test]
fn every_boundary_and_odd_value_answers_without_panic() {
    for cp in [0, 0x7F, 0x80, 0xD800, 0x10FFFF, 0x110000, u32::MAX] {
        assert_eq!(lookup(&[cp]), None);
        let mut long = cps("CJK UNIFIED IDEOGRAPH-");
        long.push(cp);
        assert_eq!(lookup(&long), None);
    }
    for odd in [
        "CJK UNIFIED IDEOGRAPH-FFFFFFFFFFFFFFFF",
        "CJK UNIFIED IDEOGRAPH-10FFFF",
        "CJK UNIFIED IDEOGRAPH-FFFFFF",
        "TANGUT IDEOGRAPH-",
        "HANGUL SYLLABLE ",
        "HANGUL SYLLABLE GGGGGGGGGGGGGGGG",
        "-",
        ",",
        ".",
        "",
    ] {
        assert_eq!(lookup(&cps(odd)), None, "{odd}");
    }
}

#[test]
fn a_name_of_100000_characters() {
    let mut name = cps("HANGUL SYLLABLE ");
    name.extend(std::iter::repeat_n(u32::from(b'G'), 100_000));
    assert_eq!(lookup(&name), None);
    let long = vec![u32::from(b'A'); 100_000];
    assert_eq!(lookup(&long), None);
    let mut text = cps("'\\N{");
    text.extend(&long);
    text.extend(cps("}'"));
    assert!(literal_tokens(&text).is_err());
}

#[test]
fn a_hundred_thousand_escapes() {
    // Unclosed: the first escape searches to the end and raises.
    let mut text = cps("'");
    for _ in 0..100_000 {
        text.extend(cps("\\N{"));
    }
    text.extend(cps("'"));
    assert!(literal_tokens(&text).is_err());
    // Each closed and known: a str of 100,000 em dashes.
    let mut text = cps("'");
    for _ in 0..100_000 {
        text.extend(cps("\\N{EM DASH}"));
    }
    text.extend(cps("'"));
    let lexed = literal_tokens(&text).expect("decodes");
    assert_eq!(lexed.tokens.len(), 1);
}

#[test]
fn every_name_looks_up_in_time_linear_in_the_count() {
    let names = every_name();
    for (name, cp) in &names {
        assert_eq!(lookup(name), Some(*cp));
    }
    let large: Vec<(Vec<u32>, u32)> = names.iter().take(50_000).cloned().collect();
    let small: Vec<(Vec<u32>, u32)> = large.iter().step_by(10).cloned().collect();
    let run = |set: &[(Vec<u32>, u32)]| {
        timing::once(|| {
            for (name, cp) in set {
                assert_eq!(lookup(name), Some(*cp));
            }
        })
    };
    timing::grows_within(
        "lookup: 5,000 and 50,000 names",
        timing::TENFOLD,
        &mut || run(&small),
        &mut || run(&large),
    );
}
