//! Hostile input for the capture rules.
//!
//! `_DRIFTING_COUNT_RE` and `_TEST_TOTAL_RE` are quadratic in the
//! reference (CPython 3.13.13's `re` takes 9.2 s and 2.7 s at 32,000
//! characters of `1.` and `1,` repeated), and `_COMMIT_HASH_RE` holds an
//! unbounded repeat inside each lookahead. On the engine their steps stay
//! within a pinned multiple of the text's length at 10,000, 100,000 and a
//! megabyte, and the steps at twice a length are within 2.2 times those at
//! the length. `find_capture_rule_hits` over the same bodies, over
//! unpaired and dense quotes, and over lone surrogates answers without a
//! panic, ten times the body within thirty times the time.
//!
//! One family is quadratic in the reference and here alike: a sentence
//! holding an ISO date and many counts, where `keep` reads the whole
//! sentence again for every count it refuses. It is held to that family.

mod timing;

use bettermemory_core::durability::{find_capture_rule_hits, CAPTURE_PATTERNS};
use bettermemory_core::pyre::memo;
use bettermemory_core::pyre::regex::Regex;
use bettermemory_core::unicode::cps;

/// `_DRIFTING_COUNT_RE.finditer` steps per code point, at most (118.5
/// measured on `1.` repeated, as the engine's memo family measured it).
const DRIFTING_COUNT_STEPS_PER_CHAR: u64 = 119;
/// `_TEST_TOTAL_RE.finditer` steps per code point, at most (71.5 measured
/// on `1,` repeated).
const TEST_TOTAL_STEPS_PER_CHAR: u64 = 72;
/// `_COMMIT_HASH_RE.search` steps per code point, at most (5.77 measured
/// on all-letter hex runs, 4.0 on one digit run, 3.0 on `a1` repeated).
const COMMIT_HASH_STEPS_PER_CHAR: u64 = 6;

fn pattern(id: &str) -> Regex {
    let &(_, text, flags) = CAPTURE_PATTERNS
        .iter()
        .find(|(name, _, _)| *name == id)
        .expect("a capture pattern");
    Regex::new_str(text, flags).expect("the engine builds it")
}

/// `head + unit * (len / unit's length) + tail`.
fn body(head: &str, unit: &str, len: usize, tail: &str) -> Vec<u32> {
    let unit_len = unit.chars().count();
    cps(&format!("{head}{}{tail}", unit.repeat(len / unit_len)))
}

/// The steps of one call as the reference makes it: `finditer` for the two
/// count patterns, `search` for the hash.
fn steps(re: &Regex, text: &[u32], finditer: bool) -> u64 {
    if finditer {
        re.finditer(text, 0, isize::MAX).expect("no budget");
    } else {
        re.search(text, 0, isize::MAX).expect("no budget");
    }
    memo::last_call().steps
}

fn families() -> Vec<(&'static str, &'static str, &'static str, bool, u64)> {
    vec![
        (
            "_DRIFTING_COUNT_RE",
            "",
            "1.",
            true,
            DRIFTING_COUNT_STEPS_PER_CHAR,
        ),
        ("_TEST_TOTAL_RE", "", "1,", true, TEST_TOTAL_STEPS_PER_CHAR),
        (
            "_COMMIT_HASH_RE",
            "",
            "a1",
            false,
            COMMIT_HASH_STEPS_PER_CHAR,
        ),
        (
            "_COMMIT_HASH_RE",
            "",
            "abcdefabcdef-",
            false,
            COMMIT_HASH_STEPS_PER_CHAR,
        ),
        (
            "_COMMIT_HASH_RE",
            "",
            "1",
            false,
            COMMIT_HASH_STEPS_PER_CHAR,
        ),
    ]
}

#[test]
fn the_quadratic_families_take_linear_steps() {
    for (id, head, unit, finditer, per_char) in families() {
        let re = pattern(id);
        for n in [10_000, 100_000, 500_000] {
            let small = body(head, unit, n, "");
            let large = body(head, unit, 2 * n, "");
            let s = steps(&re, &small, finditer);
            let l = steps(&re, &large, finditer);
            println!("{id} {unit:?} {n}: {s} steps, {l} at 2n");
            for (len, got) in [(small.len(), s), (large.len(), l)] {
                assert!(
                    got <= per_char * len as u64,
                    "{id} {unit:?} at {len}: {got} steps"
                );
            }
            assert!(l * 10 <= s * 22, "{id} {unit:?} {n}: {l} against {s}");
        }
    }
}

fn hit_names(text: &[u32]) -> Vec<&'static str> {
    find_capture_rule_hits(text)
        .expect("every pattern builds")
        .iter()
        .map(|h| h.name)
        .collect()
}

/// The hostile bodies through the whole check at a megabyte, with the
/// reference's answers (the same as at 2,000 characters, pinned in the
/// golden file).
#[test]
fn a_megabyte_of_each_hostile_body_answers() {
    const MB: usize = 1 << 20;
    for (head, unit, tail, want) in [
        ("", "1.", "", vec![]),
        ("", "1,", "", vec![]),
        ("tests ", "1,", " passed", vec!["test_total"]),
        ("tests 1 passed ", "a1", "", vec!["test_total"]),
        ("", "\"1 star\" ", "", vec![]),
        ("\"", "1 star ", "", vec!["drifting_count"]),
        ("", "\u{201c}", "", vec![]),
        ("", "\u{201c}recently\u{201c}\n", "", vec!["absolute_date"]),
    ] {
        let text = body(head, unit, MB, tail);
        assert_eq!(hit_names(&text), want, "{unit:?}");
    }
    let surrogates: Vec<u32> = [0x61, 0xD800, 0x20].repeat(MB / 3);
    assert!(hit_names(&surrogates).is_empty());
}

#[test]
fn the_check_is_linear_in_time() {
    for (name, head, unit) in [
        ("digit-dot run", "", "1."),
        ("digit-comma run", "tests ", "1,"),
        ("hex run", "tests 1 passed ", "a1"),
        ("dense quotes", "", "\"1 star\" "),
        ("unpaired quote", "\"", "1 star "),
        ("plain prose", "", "The cache holds the last page read. "),
    ] {
        let small = body(head, unit, 10_000, "");
        let large = body(head, unit, 100_000, "");
        timing::grows_within(
            name,
            30,
            &mut || timing::once(|| find_capture_rule_hits(&small).unwrap()),
            &mut || timing::once(|| find_capture_rule_hits(&large).unwrap()),
        );
    }
}

/// A sentence with an ISO date and a count every few words: each count
/// `keep` refuses reads the whole sentence again, in the reference as
/// here. Ten times the sentence within two hundred times the time.
#[test]
fn a_dated_sentence_of_counts_is_quadratic_as_the_reference() {
    for (name, head, unit) in [
        ("counts", "2025-01-01 ", "1 star "),
        ("totals", "tests 2025-01-01 ", "1 passed "),
    ] {
        let small = body(head, unit, 1_000, "");
        let large = body(head, unit, 10_000, "");
        assert!(hit_names(&large).is_empty());
        timing::grows_within(
            name,
            200,
            &mut || timing::once(|| find_capture_rule_hits(&small).unwrap()),
            &mut || timing::once(|| find_capture_rule_hits(&large).unwrap()),
        );
    }
}

#[test]
fn empty_and_surrogate_bodies_answer() {
    assert!(hit_names(&[]).is_empty());
    assert!(hit_names(&[0xD800]).is_empty());
    assert!(hit_names(&[0xDFFF, 0x22, 0xD800, 0x22]).is_empty());
    let mut t = cps("Tests ");
    t.push(0xD800);
    t.extend(cps(" 5 passed"));
    assert_eq!(hit_names(&t), vec!["test_total"]);
}
