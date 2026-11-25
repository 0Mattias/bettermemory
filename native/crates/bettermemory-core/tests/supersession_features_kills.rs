//! Inputs on which a plausible wrong port of the supersession features
//! answers otherwise than the reference: the cue alternation's order,
//! `\b`, `str.isdigit` and `str.split` read as ASCII, a wrong strip set,
//! the first clause word kept, the slot rules, the numeric cap, the
//! kinship floor, the context's exclusions, the compound's length floor,
//! and the word-token cache keyed on the lowered word. Each expectation is
//! the reference's answer.

use std::collections::BTreeSet;
use std::sync::{Mutex, MutexGuard};

use bettermemory_core::caches;
use bettermemory_core::search_dedup::raw_content_token_set;
use bettermemory_core::supersession::{
    anchor_tokens, anchored_values, change_cues, clear_word_token_cache, content_stream, context,
    cue_pattern, numeric_shape, numeric_token_set, proper_nouns, surface_form, surface_forms,
    value_tokens, values_are_kin, word_token_cache_len, word_tokens, SupersessionMatch,
    CHANGE_CUES,
};
use bettermemory_core::unicode::cps;

/// The cache is process-wide: the tests of this file take turns.
static SERIAL: Mutex<()> = Mutex::new(());

fn serial() -> MutexGuard<'static, ()> {
    SERIAL
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn set(items: &[&str]) -> BTreeSet<String> {
    items.iter().map(|s| (*s).to_string()).collect()
}

fn text(t: &[u32]) -> String {
    t.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

#[test]
fn the_cue_alternation_is_longest_first_in_table_order() {
    let _g = serial();
    let pattern = text(&cue_pattern());
    assert!(pattern.starts_with(r"\b(decommissioned|the\ previous|rolled\ back|instead\ of|"));
    assert!(pattern.ends_with(r"|adopts|again|moved|moves|now)\b"));
    // No cue is a prefix of another, so another order changes no match;
    // the order is pinned through the pattern's text.
    for (a, _) in CHANGE_CUES {
        for (b, _) in CHANGE_CUES {
            assert!(a == b || !b.starts_with(a), "{a} is a prefix of {b}");
        }
    }
}

#[test]
fn word_boundaries_read_unicode_words() {
    let _g = serial();
    let got: Vec<String> = change_cues(&cps("\u{e9}moved to port; moved\u{663} now"))
        .unwrap()
        .iter()
        .map(|c| text(c))
        .collect();
    assert_eq!(got, ["now"]);
    assert!(change_cues(&cps("the script removed it"))
        .unwrap()
        .is_empty());
}

#[test]
fn isdigit_reads_unicode_digits() {
    let _g = serial();
    let t = cps("Builds moved to build-\u{663} from build-\u{664}.");
    let raw = raw_content_token_set(&t);
    assert_eq!(
        anchor_tokens(&t, &raw).unwrap(),
        set(&["build-\u{663}", "build-\u{664}"])
    );
    // A two-part compound without a digit is dropped, three parts kept.
    let t = cps("Pages go on-call and read-only via billing-db-green");
    let raw = raw_content_token_set(&t);
    assert_eq!(anchor_tokens(&t, &raw).unwrap(), set(&["billing-db-green"]));
    assert_eq!(
        value_tokens(&t, &raw).unwrap(),
        set(&["billing-db-green", "on-call", "read-onli"])
    );
}

#[test]
fn split_reads_every_whitespace() {
    let _g = serial();
    for t in [
        "use\tAirflow",
        "use\u{1f}Airflow",
        "use\u{a0}Airflow",
        "use\u{2028}Airflow",
    ] {
        assert_eq!(proper_nouns(&cps(t)).unwrap(), set(&["airflow"]), "{t:?}");
    }
}

#[test]
fn the_strip_sets_are_the_references() {
    let _g = serial();
    let t = cps("We use (Airflow) 'Redis' \"Kafka\" `Postgres` [Gitlab]");
    assert_eq!(
        proper_nouns(&t).unwrap(),
        set(&["airflow", "kafka", "postgr", "redis"])
    );
    let forms = surface_forms(&cps("Use (Redis). Then redis.")).unwrap();
    let forms: Vec<(String, String)> = forms.into_iter().map(|(k, v)| (k, text(&v))).collect();
    assert_eq!(
        forms,
        [
            ("use".to_string(), "Use".to_string()),
            ("redis".to_string(), "Redis".to_string())
        ]
    );
    assert_eq!(
        surface_form(&cps("Use (Redis)."), "redis")
            .unwrap()
            .map(|f| text(&f)),
        Some("Redis".to_string())
    );
    assert!(numeric_token_set(&cps("._9_.")).unwrap().contains("9"));
}

#[test]
fn the_first_word_of_a_clause_is_skipped() {
    let _g = serial();
    assert!(proper_nouns(&cps("Airflow runs. Redis too"))
        .unwrap()
        .is_empty());
    assert_eq!(
        proper_nouns(&cps("We use Airflow")).unwrap(),
        set(&["airflow"])
    );
}

#[test]
fn the_slot_rules() {
    let _g = serial();
    let anchored = |t: &str| anchored_values(&cps(t)).unwrap();
    // The preposition must be among the first three words.
    assert!(anchored("Paging moved quickly and quietly to Opsgenie").is_empty());
    // The first content token after it, and no further.
    assert_eq!(anchored("Paging moved to the new PagerDuty"), set(&["new"]));
    // A direct cue reads the first three words only.
    assert!(anchored("Builds adopted the a an yarn").is_empty());
    assert_eq!(anchored("Builds adopted the yarn"), set(&["yarn"]));
    // The slot ends with the clause.
    assert!(anchored("Paging moved. To Opsgenie").is_empty());
}

#[test]
fn numeric_tokens_are_lowered_and_capped_at_16() {
    let _g = serial();
    assert_eq!(
        numeric_token_set(&cps("\u{212a}8S 1234567890123456 12345678901234567")).unwrap(),
        set(&["1234567890123456", "k8s"])
    );
    assert!(numeric_token_set(&cps("\u{663}\u{664}"))
        .unwrap()
        .is_empty());
    assert_eq!(
        numeric_shape(&cps("\u{668}\u{664}\u{664}\u{663}")).unwrap(),
        Some("int:4".to_string())
    );
    assert_eq!(
        numeric_shape(&cps("3.13.0")).unwrap(),
        Some("dotted:2".to_string())
    );
    assert_eq!(numeric_shape(&cps("3.")).unwrap(), None);
}

#[test]
fn kinship_floors() {
    let _g = serial();
    assert!(!values_are_kin("the-api", "the-web").unwrap());
    assert!(values_are_kin("eu-west-1", "eu-central-1").unwrap());
    assert!(!values_are_kin("x-y-z", "x-q-r-s").unwrap());
    assert!(values_are_kin("x-y-z", "x-y-r-s").unwrap());
    assert!(values_are_kin("\u{668}\u{664}\u{664}\u{663}", "9443").unwrap());
    assert!(!values_are_kin("8443", "a-b").unwrap());
    assert!(!values_are_kin("8443", "8443").unwrap());
}

#[test]
fn context_leaves_out_the_value_and_the_cue_words() {
    let _g = serial();
    let stream = content_stream(&cps("moved port 9443 to new host previously old box"));
    assert_eq!(context(&stream, "9443"), set(&["host", "port"]));
    assert_eq!(context(&stream, "host"), set(&["9443", "box", "port"]));
    // A value within CONTEXT_WIDTH of another occurrence of itself is
    // still left out.
    let twice = |s: &[&str]| s.iter().map(|t| (*t).to_string()).collect::<Vec<_>>();
    assert_eq!(context(&twice(&["a", "x", "a"]), "a"), set(&["x"]));
}

#[test]
fn a_compound_holds_more_than_two_characters() {
    let _g = serial();
    // `value_tokens` takes any set: a two-character token with a `-` is
    // no compound, a three-character one is.
    let none = cps("");
    assert!(value_tokens(&none, &set(&["a-"])).unwrap().is_empty());
    assert!(value_tokens(&none, &set(&["-1"])).unwrap().is_empty());
    assert!(anchor_tokens(&none, &set(&["-1"])).unwrap().is_empty());
    assert_eq!(value_tokens(&none, &set(&["ab-"])).unwrap(), set(&["ab-"]));
    assert!(anchor_tokens(&none, &set(&["ab-"])).unwrap().is_empty());
}

#[test]
fn the_note_and_the_detector() {
    let _g = serial();
    let m = SupersessionMatch {
        memory_id: cps("m"),
        outcome: cps("conflicts"),
        evidence: cps("similarity"),
        cue: None,
        new_value: cps("it's"),
        old_value: cps("3.11"),
        similarity: 0.3,
        summary: cps(""),
    };
    assert_eq!(
        text(&m.note()),
        "set at write time: None in this memory, \"it's\" against '3.11' (similarity)"
    );
    assert_eq!(m.detector().unwrap(), "value");
    // A backslash is doubled by `repr` (the golden files hold no run of
    // backslashes, which the host-fact guard reads as a UNC path).
    let slash = SupersessionMatch {
        cue: Some(cps("now")),
        new_value: cps("a\\b"),
        ..m.clone()
    };
    assert_eq!(
        text(&slash.note()),
        "set at write time: 'now' in this memory, 'a\\\\b' against '3.11' (similarity)"
    );
    let m = SupersessionMatch {
        new_value: cps("\u{663}"),
        old_value: cps("9"),
        ..m
    };
    assert_eq!(m.detector().unwrap(), "numeric");
}

#[test]
fn the_cache_is_keyed_on_the_word_as_given() {
    let _g = serial();
    clear_word_token_cache();
    assert_eq!(word_token_cache_len(), 0);
    let upper = word_tokens(&cps("Qzxfoo"));
    let lower = word_tokens(&cps("qzxfoo"));
    assert_eq!(upper, lower);
    assert_eq!(word_token_cache_len(), 2);
    assert!(caches::keys().contains(&"supersession.clear_word_token_cache"));
    caches::clear_all();
    assert_eq!(word_token_cache_len(), 0);
}
