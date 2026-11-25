//! Hostile input for the supersession features. `_PROPER_NOUN_RE` is
//! quadratic in the reference on a long capitalised word that fails at
//! its end (CPython 3.13.13 takes 0.19 s at 10,000 characters and 0.76 s
//! at 20,000); on the engine its steps are within a pinned multiple of
//! the word's length and grow linearly (rule 6: steps at 2n within 2.2
//! times steps at n). `_CUE_RE` over bodies dense in cue prefixes is
//! linear too. A megabyte body, lone surrogates and 70,000 distinct words
//! through the word-token cache (emptied once) answer without a panic.

mod timing;

use std::sync::{Mutex, MutexGuard};

use bettermemory_core::pyre::memo;
use bettermemory_core::pyre::regex::Regex;
use bettermemory_core::search_dedup::raw_content_token_set;
use bettermemory_core::supersession::{
    anchor_tokens, anchored_values, change_cues, clear_word_token_cache, content_stream, context,
    cue_pattern, numeric_token_set, proper_nouns, surface_forms, value_tokens,
    word_token_cache_len, word_tokens, PROPER_NOUN_PATTERN, WORD_TOKEN_ENTRIES,
};
use bettermemory_core::unicode::cps;

/// `_PROPER_NOUN_RE.match` steps per code point of the word, at most
/// (70.3 measured on the hostile words below).
const PROPER_NOUN_STEPS_PER_CHAR: u64 = 75;
/// `_CUE_RE.finditer` steps per code point of the body, at most (98.3
/// measured, on `no ` repeated).
const CUE_STEPS_PER_CHAR: u64 = 100;

/// The cache is process-wide: the tests of this file take turns.
static SERIAL: Mutex<()> = Mutex::new(());

fn serial() -> MutexGuard<'static, ()> {
    SERIAL
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// The words `_proper_nouns` hands to `_PROPER_NOUN_RE.match`, `n` long,
/// that fail at the end.
fn hostile_words(n: usize) -> Vec<(&'static str, String)> {
    vec![
        ("capitals", format!("{}+", "A".repeat(n))),
        ("pairs", format!("{}+", "Aa".repeat(n / 2))),
        ("lower run", format!("A{}+", "a".repeat(n))),
        ("digits", format!("A{}+", "9".repeat(n))),
        ("capitals then lower", format!("{}a+", "A".repeat(n))),
    ]
}

fn proper_noun_steps(word: &str) -> u64 {
    let re = Regex::new_str(PROPER_NOUN_PATTERN, 0).expect("the engine builds the pattern");
    let found = re
        .r#match(cps(word).as_slice(), 0, isize::MAX)
        .expect("no budget");
    assert!(found.is_none());
    memo::last_call().steps
}

fn cue_steps(body: &str) -> u64 {
    let re = Regex::new(&cue_pattern(), 0).expect("the engine builds the pattern");
    re.finditer(cps(body).as_slice(), 0, isize::MAX)
        .expect("no budget");
    memo::last_call().steps
}

fn cue_bodies(n: usize) -> Vec<(&'static str, String)> {
    let fill = |unit: &str| unit.repeat(n / unit.len());
    vec![
        ("letters", fill("a")),
        ("no", fill("no ")),
        ("the", fill("the ")),
        ("glued cues", fill("moved")),
        ("cue prefixes", fill("the previou cut ove rolled bac ")),
        ("cues", fill("moved to ")),
    ]
}

#[test]
fn proper_noun_steps_are_linear() {
    for n in [10_000, 100_000] {
        for (name, word) in hostile_words(n) {
            let small = proper_noun_steps(&word);
            let (_, large_word) = hostile_words(2 * n)
                .into_iter()
                .find(|(k, _)| *k == name)
                .expect("the same shape");
            let large = proper_noun_steps(&large_word);
            let len = word.chars().count() as u64;
            println!("proper noun {name} {n}: {small} steps, {large} at 2n");
            assert!(
                small <= PROPER_NOUN_STEPS_PER_CHAR * len,
                "{name} {n}: {small}"
            );
            assert!(
                large * 10 <= small * 22,
                "{name} {n}: {large} against {small}"
            );
        }
    }
}

#[test]
fn cue_steps_are_linear() {
    for n in [10_000, 100_000] {
        for ((name, body), (_, large_body)) in cue_bodies(n).into_iter().zip(cue_bodies(2 * n)) {
            let small = cue_steps(&body);
            let large = cue_steps(&large_body);
            let len = body.chars().count() as u64;
            println!("cue {name} {n}: {small} steps, {large} at 2n");
            assert!(small <= CUE_STEPS_PER_CHAR * len, "{name} {n}: {small}");
            assert!(
                large * 10 <= small * 22,
                "{name} {n}: {large} against {small}"
            );
        }
    }
}

#[test]
fn the_features_are_linear_in_time() {
    let _g = serial();
    for (name, unit) in [
        ("capitals", "We use AAAAAAAAAAAAAAAAAAA+ "),
        ("cues", "moved to port 9443 then; "),
        ("compounds", "a-b-c-1 x-y v2.3 "),
    ] {
        let small = cps(&unit.repeat(10_000 / unit.len()));
        let large = cps(&unit.repeat(100_000 / unit.len()));
        let run = |t: &[u32]| {
            let raw = raw_content_token_set(t);
            (
                anchored_values(t).unwrap(),
                anchor_tokens(t, &raw).unwrap(),
                surface_forms(t).unwrap(),
            )
        };
        timing::grows_within(name, 30, &mut || timing::once(|| run(&small)), &mut || {
            timing::once(|| run(&large))
        });
    }
}

/// `anchored_values` reads, per cue, the words of the rest of its clause
/// (`_CLAUSE_RE.split` and `_WORD_RE.findall` over the slice after the
/// cue), so one clause holding a cue every few words costs the square of
/// its length, in the reference as here (CPython 3.13.13: 0.077 s at
/// 10,000 characters, 7.6 s at 100,000). The port is held to that
/// family: ten times the clause within 200 times the time.
#[test]
fn anchored_values_is_quadratic_in_a_clause_of_cues_as_the_reference() {
    let _g = serial();
    let unit = "moved to port 9443 then ";
    let small = cps(&unit.repeat(1_000 / unit.len()));
    let large = cps(&unit.repeat(10_000 / unit.len()));
    timing::grows_within(
        "a clause of cues",
        200,
        &mut || timing::once(|| anchored_values(&small).unwrap()),
        &mut || timing::once(|| anchored_values(&large).unwrap()),
    );
}

#[test]
fn a_megabyte_body_answers() {
    let _g = serial();
    let unit = "Paging moved to Opsgenie-2 from PagerDuty at port 9443; ";
    let body = cps(&unit.repeat(1_000_000 / unit.len()));
    let raw = raw_content_token_set(&body);
    assert_eq!(change_cues(&body).unwrap().len(), 1_000_000 / unit.len());
    assert!(anchored_values(&body).unwrap().contains("opsgeni-2"));
    assert!(value_tokens(&body, &raw).unwrap().contains("9443"));
    assert!(numeric_token_set(&body).unwrap().contains("opsgenie-2"));
    let stream = content_stream(&body);
    assert!(context(&stream, "9443").contains("port"));
    assert_eq!(surface_forms(&body).unwrap().len(), 6);
}

#[test]
fn lone_surrogates_and_empty_text_answer() {
    let _g = serial();
    let mut body = cps("We use Airflow ");
    body.push(0xD800);
    body.extend(cps(" moved to 9443 "));
    body.push(0xDFFF);
    assert!(proper_nouns(&body).unwrap().contains("airflow"));
    assert!(change_cues(&body).unwrap().len() == 1);
    assert!(proper_nouns(&[]).unwrap().is_empty());
    assert!(anchored_values(&[0xD800]).unwrap().is_empty());
    let _ = word_tokens(&[0xDFFF]);
}

#[test]
fn the_cache_empties_whole_when_full() {
    let _g = serial();
    clear_word_token_cache();
    for i in 0..70_000 {
        word_tokens(&cps(&format!("w{i}x")));
    }
    assert_eq!(word_token_cache_len(), 70_000 - WORD_TOKEN_ENTRIES);
    clear_word_token_cache();
}
