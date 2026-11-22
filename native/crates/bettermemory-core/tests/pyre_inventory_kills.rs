//! Inputs on which a plausible wrong engine answers otherwise than `re`,
//! each on a pattern of the reference: the mutants `gen_pyre_inventory`
//! rewrites each pattern into (greedy and lazy swapped, a bound off by one,
//! `$` for `\Z`, a `\b` dropped, a flag dropped, ASCII classes), with the
//! first text of the pattern's committed texts on which the mutant differs
//! and the reference's answer written out.

mod pyre_api_support;

use bettermemory_core::pyre::regex::Regex;
use pyre_api_support::{line, Built};

/// `(pattern, bytes, flags, text, method, the reference's answer)`.
const KILLS: [(&str, bool, u32, &str, &str, &str); 10] = [
    // lazy: the mutant (?:[!?;:\n]|(?<!\d)\.|\.(?!\d))+? under flags 0 answers 0,1|-1.
    (
        "(?:[!?;:\\n]|(?<!\\d)\\.|\\.(?!\\d))+",
        false,
        0,
        "!!",
        "search",
        "0,2|-1",
    ),
    // bound: the mutant (?P<id>[0-9a-f]{33})(?:\.[0-9a-f]{16})?\.key under flags 0 answers -.
    (
        "(?P<id>[0-9a-f]{32})(?:\\.[0-9a-f]{16})?\\.key",
        false,
        0,
        "ee5a05cbd2a51d13fada458c70bf2ba9.key",
        "search",
        "0,36|0,32|1",
    ),
    // lazy: the mutant (archive|builds|environments)-v\d+? under flags 0 answers 0,9|0,6|1.
    (
        "(archive|builds|environments)-v\\d+",
        false,
        0,
        "builds-v777builds-v7777archive-v77 ",
        "search",
        "0,11|0,6|1",
    ),
    // bound: the mutant .*-[0-9A-HJKMNP-TV-Z]{27}\.md$ under flags 0 answers -.
    (
        ".*-[0-9A-HJKMNP-TV-Z]{26}\\.md$",
        false,
        0,
        "I-DJKVMFMKWMWKN31XGM2EJNZ1TJ.md",
        "search",
        "0,31|-1",
    ),
    // boundary: the mutant (?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b under flags 0 answers 1,13|-1.
    (
        "\\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\\d)[0-9a-f]{7,40}\\b",
        false,
        0,
        "ge6fa7aea2f49:e8b73a7ee624991a ",
        "search",
        "14,30|-1",
    ),
    // boundary: the mutant \b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40} under flags 0 answers 0,12|-1.
    (
        "\\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\\d)[0-9a-f]{7,40}\\b",
        false,
        0,
        "bf67da2ec956_",
        "search",
        "-",
    ),
    // flag: the mutant (?<!\w)\.net(?!\w) under flags 0 answers -.
    (
        "(?<!\\w)\\.net(?!\\w)",
        false,
        2,
        ".NeT.e",
        "search",
        "0,4|-1",
    ),
    // flag: the mutant (?<!\w)c#(?!\w) under flags 0 answers -.
    ("(?<!\\w)c#(?!\\w)", false, 2, "c#c#C#", "search", "4,6|-1"),
    // end: the mutant ^sha:[0-9a-f]{7}\Z under flags 0 answers -.
    (
        "^sha:[0-9a-f]{7}$",
        false,
        0,
        "sha:abcdef0\n",
        "search",
        "0,11|-1",
    ),
    // ascii: the mutant (?<!\w)c#(?!\w) under flags 258 answers 1,3|-1.
    ("(?<!\\w)c#(?!\\w)", false, 2, "éc#", "search", "-"),
];

#[test]
fn each_mutant_of_an_inventory_pattern_is_told_apart() {
    for (pattern, bytes, flags, text, method, want) in KILLS {
        let re = if bytes {
            Regex::new_bytes(pattern.as_bytes(), flags)
        } else {
            Regex::new_str(pattern, flags)
        }
        .unwrap_or_else(|e| panic!("{pattern}: {e:?}"));
        let b = Built { re, bytes };
        let t: Vec<u32> = text.chars().map(u32::from).collect();
        let got = line(&b, &t, 0, t.len() as isize, method, false);
        assert_eq!(got, want, "{pattern} {method} {text:?}");
    }
}

/// `$` matches before a final newline and `\Z` does not; under `match`
/// with an `endpos` the newline left out is not there.
#[test]
fn the_end_anchor_takes_a_final_newline() {
    let re = Regex::new_str("^sha:[0-9a-f]{7}$", 0).expect("built");
    let b = Built { re, bytes: false };
    let t: Vec<u32> = "sha:abcdef0\n".chars().map(u32::from).collect();
    assert_eq!(line(&b, &t, 0, 12, "fullmatch", false), "-");
    assert_eq!(line(&b, &t, 0, 12, "match", false), "0,11|-1");
    assert_eq!(line(&b, &t, 0, 11, "fullmatch", false), "0,11|-1");
}

/// A lazy repeat of one unit stops at its maximum: `a{1,2}?b` searched on
/// `aaab` matches from the second `a` (`re` answers `(1, 4)`), where a
/// repeat allowed one unit past its maximum matches from the first.
#[test]
fn a_lazy_repeat_stops_at_its_maximum() {
    let re = Regex::new_str("a{1,2}?b", 0).expect("built");
    let b = Built { re, bytes: false };
    let t: Vec<u32> = "aaab".chars().map(u32::from).collect();
    assert_eq!(line(&b, &t, 0, 4, "search", false), "1,4|-1");
    assert_eq!(line(&b, &t, 0, 4, "match", false), "-");
    assert_eq!(line(&b, &t, 1, 4, "match", false), "1,4|-1");
}

/// A class's last member below 256 is in its bitmap: `[\x01\x03\x05\xff]`,
/// which `_optimize_charset` writes as a CHARSET bitmap, matches U+00FF
/// and the byte 0xFF (`re` answers `(0, 1)` for both), where a test of the
/// bitmap below 255 drops it.
#[test]
fn a_class_holds_its_member_at_0xff() {
    let members = [0x5B, 0x01, 0x03, 0x05, 0xFF, 0x5D];
    let pattern: Vec<u32> = members.iter().map(|&c| u32::from(c)).collect();
    let text = Built {
        re: Regex::new(&pattern, 0).expect("built"),
        bytes: false,
    };
    let bytes = Built {
        re: Regex::new_bytes(&members, 0).expect("built"),
        bytes: true,
    };
    for b in [&text, &bytes] {
        assert_eq!(line(b, &[0xFF], 0, 1, "search", false), "0,1|-1");
        assert_eq!(line(b, &[0xFE, 0xFF], 0, 2, "findall", false), "['255]");
    }
}
