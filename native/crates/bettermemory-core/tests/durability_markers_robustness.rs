//! Hostile input for the transient markers.
//!
//! The patterns with an unbounded repeat after a literal head (the two
//! idioms' `\w+ing\b`, `temporarily`'s `\s+\w+s\b` lookahead, the dated
//! form's digit runs) take steps within a pinned multiple of the text's
//! length at 10,000, 100,000 and 500,000 code points, and the steps at
//! twice a length are within 2.2 times those at the length.
//!
//! `find_transient_markers` answers over a million code points dense in
//! phrases, in quoted phrases, in anchored `the new` sentences, in unpaired
//! quotes and in lone surrogates without a panic. Two of those families
//! are quadratic in the reference and linear here: a quoted match is
//! tested against every span there (`any` over the spans) and against one
//! here (a binary search over spans in text order), and the sentence of
//! each `the new` is found there by a scan from the start of the body
//! (`finditer(text, 0, pos)`) and here by a scan back to the previous
//! terminator. Both are held linear: ten times the body within `TENFOLD`
//! times the time. One family is quadratic in both: one long sentence of
//! anchored `the new`s, where every match reads the whole sentence again
//! (`sentence_around`, the cues, the tokens). It is held to that family,
//! ten times the sentence within two hundred times the time.

mod timing;

use bettermemory_core::durability::{find_transient_markers, phrase_pattern, AS_OF_DATE_RE};
use bettermemory_core::pyre::memo;
use bettermemory_core::pyre::regex::Regex;
use bettermemory_core::unicode::{cps, to_string};

/// `in the middle of` and `halfway through` steps per code point, at most
/// (6.05 measured on `halfway through the ` repeated, 4.62 on the idiom
/// before a word of eight letters, 4.00 on one long word).
const IDIOM_STEPS_PER_CHAR: u64 = 7;
/// `temporarily` steps per code point, at most (4.00 measured on one long
/// word after it, 2.47 on `temporarily aaaaaa ` repeated).
const TEMPORARILY_STEPS_PER_CHAR: u64 = 5;
/// `_AS_OF_DATE_RE` steps per code point, at most (2.69 measured on a date
/// one digit short, repeated; 1.00 on one digit run).
const AS_OF_STEPS_PER_CHAR: u64 = 3;

fn pattern(phrase: &str) -> Regex {
    if phrase == "_AS_OF_DATE_RE" {
        return Regex::new_str(AS_OF_DATE_RE.0, AS_OF_DATE_RE.1).expect("the engine builds it");
    }
    let (text, flags) = phrase_pattern(phrase);
    Regex::new(&text, flags).expect("the engine builds it")
}

/// `head + unit * (len / unit's length) + tail`.
fn body(head: &str, unit: &str, len: usize, tail: &str) -> Vec<u32> {
    let unit_len = unit.chars().count();
    cps(&format!("{head}{}{tail}", unit.repeat(len / unit_len)))
}

fn steps(re: &Regex, text: &[u32]) -> u64 {
    re.finditer(text, 0, isize::MAX).expect("no budget");
    memo::last_call().steps
}

fn families() -> Vec<(&'static str, &'static str, &'static str, u64)> {
    vec![
        (
            "in the middle of",
            "in the middle of ",
            "a",
            IDIOM_STEPS_PER_CHAR,
        ),
        (
            "in the middle of",
            "",
            "in the middle of aaaaaaaa ",
            IDIOM_STEPS_PER_CHAR,
        ),
        (
            "halfway through",
            "halfway through ",
            "a",
            IDIOM_STEPS_PER_CHAR,
        ),
        (
            "halfway through",
            "",
            "halfway through the ",
            IDIOM_STEPS_PER_CHAR,
        ),
        (
            "temporarily",
            "temporarily ",
            "a",
            TEMPORARILY_STEPS_PER_CHAR,
        ),
        (
            "temporarily",
            "",
            "temporarily aaaaaa ",
            TEMPORARILY_STEPS_PER_CHAR,
        ),
        (
            "_AS_OF_DATE_RE",
            "",
            "as of 1111-11-1 ",
            AS_OF_STEPS_PER_CHAR,
        ),
        ("_AS_OF_DATE_RE", "as of ", "1", AS_OF_STEPS_PER_CHAR),
    ]
}

#[test]
fn the_repeats_take_linear_steps() {
    for (phrase, head, unit, per_char) in families() {
        let re = pattern(phrase);
        for n in [10_000, 100_000, 500_000] {
            let small = body(head, unit, n, "");
            let large = body(head, unit, 2 * n, "");
            let s = steps(&re, &small);
            let l = steps(&re, &large);
            println!(
                "{phrase} {unit:?} {n}: {s} steps ({:.2}/cp), {l} at 2n ({:.2}/cp)",
                s as f64 / small.len() as f64,
                l as f64 / large.len() as f64
            );
            for (len, got) in [(small.len(), s), (large.len(), l)] {
                assert!(
                    got <= per_char * len as u64,
                    "{phrase} {unit:?} at {len}: {got} steps"
                );
            }
            assert!(l * 10 <= s * 22, "{phrase} {unit:?} {n}: {l} against {s}");
        }
    }
}

fn marker_names(text: &[u32]) -> Vec<String> {
    find_transient_markers(text)
        .expect("every pattern builds")
        .iter()
        .map(|h| to_string(&h.marker).unwrap())
        .collect()
}

/// A sentence with a cue and an identifier around one `the new`.
const ANCHORED: &str = "We moved to v2 with the new flow. ";
/// The same in one sentence that never ends.
const ANCHORED_RUN: &str = "we moved to v2 with the new flow, ";

/// The families of the time claims: name, head, unit.
fn bodies() -> Vec<(&'static str, &'static str, &'static str)> {
    vec![
        (
            "dense phrases",
            "",
            "it is currently the new plan for now. ",
        ),
        (
            "dense quoted phrases",
            "",
            "\"currently\" \"today\" \"the new\" ",
        ),
        ("anchored sentences", "", ANCHORED),
        ("unpaired quotes", "\"", "currently today "),
        ("plain prose", "", "The cache holds the last page read. "),
    ]
}

/// The families at a megabyte, with the reference's answers (the same as
/// at a few hundred code points, where the reference was run).
#[test]
fn a_megabyte_of_each_body_answers() {
    const MB: usize = 1 << 20;
    let want: [&[&str]; 5] = [
        &["currently", "for now", "the new"],
        &[],
        &[],
        &["currently", "today"],
        &[],
    ];
    for ((name, head, unit), want) in bodies().into_iter().zip(want) {
        let text = body(head, unit, MB, "");
        assert_eq!(marker_names(&text), want, "{name}");
    }
    let surrogates: Vec<u32> = [0x61, 0xD800, 0x20].repeat(MB / 3);
    assert!(marker_names(&surrogates).is_empty());
    let mut unit = vec![0xD800];
    unit.extend(cps(" \"today\" "));
    let mut tail = unit.repeat(MB / unit.len());
    tail.extend(cps(" as of 2025-01-01"));
    assert_eq!(marker_names(&tail), vec!["as of <date>"]);
}

#[test]
fn the_check_is_linear_in_time() {
    for (name, head, unit) in bodies() {
        let small = body(head, unit, 10_000, "");
        let large = body(head, unit, 100_000, "");
        timing::grows_within(
            name,
            timing::TENFOLD,
            &mut || timing::once(|| find_transient_markers(&small).unwrap()),
            &mut || timing::once(|| find_transient_markers(&large).unwrap()),
        );
    }
}

/// One sentence of anchored `the new`s: each match reads the whole
/// sentence again, in the reference as here.
#[test]
fn one_sentence_of_anchored_matches_is_quadratic_as_the_reference() {
    let small = body("", ANCHORED_RUN, 1_000, "");
    let large = body("", ANCHORED_RUN, 10_000, "");
    assert!(marker_names(&large).is_empty());
    timing::grows_within(
        "anchored run",
        200,
        &mut || timing::once(|| find_transient_markers(&small).unwrap()),
        &mut || timing::once(|| find_transient_markers(&large).unwrap()),
    );
}

#[test]
fn empty_and_surrogate_bodies_answer() {
    assert!(marker_names(&[]).is_empty());
    assert!(marker_names(&[0xD800]).is_empty());
    assert!(marker_names(&[0xDFFF, 0x22, 0xD800, 0x22]).is_empty());
    let mut t = cps("it is ");
    t.push(0xD800);
    t.extend(cps(" currently"));
    assert_eq!(marker_names(&t), vec!["currently"]);
}
