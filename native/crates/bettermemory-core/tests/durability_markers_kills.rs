//! Cases a plausible wrong reading of the transient markers fails, each
//! with the answer CPython 3.13.13 gives (`durability.find_transient_markers`).

use bettermemory_core::durability::{
    anchored_new_reference, find_transient_markers, phrase_pattern, PATTERN_OVERRIDES,
    TITLECASE_SKIP_MARKERS, TRANSIENT_PHRASE_MARKERS,
};
use bettermemory_core::pyre::regex::Regex;
use bettermemory_core::unicode::{cps, to_string};

fn hits(body: &str) -> Vec<(String, String)> {
    find_transient_markers(&cps(body))
        .expect("every pattern builds")
        .iter()
        .map(|h| {
            (
                to_string(&h.marker).unwrap(),
                to_string(&h.snippet).unwrap(),
            )
        })
        .collect()
}

fn one(marker: &str, snippet: &str) -> Vec<(String, String)> {
    vec![(marker.to_owned(), snippet.to_owned())]
}

fn markers(body: &str) -> Vec<String> {
    hits(body).into_iter().map(|(m, _)| m).collect()
}

#[test]
fn every_pattern_builds_on_the_engine() {
    for phrase in TRANSIENT_PHRASE_MARKERS {
        let (text, flags) = phrase_pattern(phrase);
        assert!(Regex::new(&text, flags).is_ok(), "{phrase}");
    }
}

/// The generic text is `\b` + `re.escape(phrase)` + `\b(?!\.\w)`: 3.13's
/// `re.escape` escapes a space. An override replaces it whole, and the
/// table keeps one entry per phrase.
#[test]
fn the_texts_are_the_reference_s() {
    assert_eq!(
        phrase_pattern("as of now").0,
        cps(r"\bas\ of\ now\b(?!\.\w)")
    );
    assert_eq!(phrase_pattern("the new").0, cps(r"\b[Tt]he new\b"));
    assert_eq!(phrase_pattern("the new").1, 0);
    for &(phrase, text, _) in &PATTERN_OVERRIDES {
        assert!(TRANSIENT_PHRASE_MARKERS.contains(&phrase));
        assert_eq!(phrase_pattern(phrase).0, cps(text));
    }
    for (i, a) in TRANSIENT_PHRASE_MARKERS.iter().enumerate() {
        assert!(!TRANSIENT_PHRASE_MARKERS[i + 1..].contains(a), "{a}");
    }
    for word in TITLECASE_SKIP_MARKERS {
        assert!(TRANSIENT_PHRASE_MARKERS.contains(&word));
    }
}

/// `continue` past a quoted or title-case match, not `break`: the later
/// unquoted use fires.
#[test]
fn a_skipped_match_lets_the_next_one_through() {
    assert_eq!(
        hits("\"currently\" we are currently here"),
        one("currently", "\"currently\" we are currently here")
    );
    assert_eq!(
        hits("Tomorrow Night and tomorrow"),
        one("tomorrow", "Tomorrow Night and tomorrow")
    );
    assert_eq!(
        hits("\u{201c}today\u{201d} and Today Matters and today"),
        one("today", "...d Today Matters and today")
    );
    assert_eq!(
        hits("We moved to Bugsnag with the new SDK! Then the new cache landed."),
        one("the new", "...h the new SDK! Then the new cache landed.")
    );
}

/// The title-case skip holds for the time words only.
#[test]
fn the_title_case_skip_is_for_time_words_only() {
    assert_eq!(
        hits("the Currently Show"),
        one("currently", "the Currently Show")
    );
    assert_eq!(markers("the Right Now Show"), vec!["right now"]);
    assert!(hits("the Tomorrow Night theme").is_empty());
    assert!(hits("This Week in Rust is a newsletter").is_empty());
}

/// The anchored test reads the match's sentence, not the body.
#[test]
fn the_anchored_test_reads_the_sentence() {
    assert_eq!(
        hits("We switched to Bugsnag. The new schema is live."),
        one("the new", "...witched to Bugsnag. The new schema is live.")
    );
    assert!(hits("We switched to Bugsnag with the new SDK release.").is_empty());
    assert_eq!(
        hits("the new thing. We moved to Bugsnag with the new SDK."),
        one("the new", "the new thing. We moved to...")
    );
    let body = cps("We switched to Bugsnag. The new schema is live.");
    assert!(!anchored_new_reference(&body, 24).unwrap());
    assert!(anchored_new_reference(&cps("We moved 3.13 with the new parser"), 19).unwrap());
    // A cue without an identifier, an identifier without a cue.
    assert_eq!(
        markers("The new schema replaces the old layout."),
        vec!["the new"]
    );
    assert_eq!(markers("Bugsnag ships the new SDK."), vec!["the new"]);
}

/// Each override replaces the generic pattern: what the generic pattern
/// would fire on stays silent, and what it would refuse fires.
#[test]
fn an_override_replaces_the_generic_pattern() {
    for body in [
        "today's date",
        "The New York office",
        "THE NEW SCHEMA",
        "the New schema",
        "at the moment of release",
        "temporarily blocks",
        "in the middle of a focus block",
        "halfway through morning",
        "the installer reboots halfway through",
    ] {
        assert!(hits(body).is_empty(), "{body}");
    }
    // `the new` has no domain lookahead.
    assert_eq!(
        hits("see the new.io site"),
        one("the new", "see the new.io site")
    );
    assert_eq!(markers("at the moment.io"), vec!["at the moment"]);
    assert_eq!(markers("temporarily.io"), vec!["temporarily"]);
    // The generic lookahead holds for every other phrase.
    assert!(hits("tomorrow.io").is_empty());
    assert_eq!(
        hits("currently.io and currently"),
        one("currently", "currently.io and currently")
    );
    assert_eq!(
        hits("Deploy tomorrow."),
        one("tomorrow", "Deploy tomorrow.")
    );
}

/// IGNORECASE is `re`'s folding of one code point against one: the Kelvin
/// sign and the long s fold, `ß` is not `ss`.
#[test]
fn ignorecase_folds_past_ascii_one_code_point_at_a_time() {
    assert_eq!(markers("next wee\u{212a} we ship"), vec!["next week"]);
    assert_eq!(markers("it is \u{17f}tashed"), vec!["is stashed"]);
    assert_eq!(markers("\u{130} just did it"), vec!["i just"]);
    assert!(hits("it is in progre\u{df}").is_empty());
    assert_eq!(markers("CURRENTLY"), vec!["currently"]);
}

/// Phrases in table order, the date last and never tested for title case.
#[test]
fn the_date_comes_last_and_has_no_title_test() {
    assert_eq!(
        hits("as of 2025-01-01 currently"),
        vec![
            (
                "currently".to_owned(),
                "as of 2025-01-01 currently".to_owned()
            ),
            (
                "as of <date>".to_owned(),
                "as of 2025-01-01 currently".to_owned()
            ),
        ]
    );
    assert_eq!(
        hits("As Of 2025-01-01 Things Changed"),
        one("as of <date>", "As Of 2025-01-01 Things Changed")
    );
    assert_eq!(
        markers("\"as of 2025-01-01\" and as of 2025-02-02"),
        vec!["as of <date>"]
    );
    assert!(hits("as of 2025-01-012").is_empty());
    assert_eq!(
        markers("the new thing, currently, today and as of 2025-01-01"),
        vec!["currently", "today", "the new", "as of <date>"]
    );
}

/// The idioms fire with an in-flight-work object only.
#[test]
fn the_idioms_want_an_object() {
    assert_eq!(
        markers("halfway through migrating"),
        vec!["halfway through"]
    );
    assert_eq!(
        markers("in the middle of the rollout"),
        vec!["in the middle of"]
    );
    assert!(hits("in the middle of evening").is_empty());
    assert!(hits("in the middle of  migrating").is_empty());
}
