//! Hostile inputs to `search_dedup`: very large bodies and record lists,
//! compounds of many parts, long tied groups, integer bounds. None may
//! panic, in debug or release, and each finishes well within the time the
//! reference takes on the same input (measured in the README).

mod common;

use std::time::{Duration, Instant};

use bettermemory_core::search_dedup::{
    find_similar, find_similar_tombstones, pairwise_content_jaccard, similarity_bound, snippet_for,
    DedupError, RawTokenCache, TokenSet,
};
use bettermemory_core::unicode::cps;
use common::dedup::Rec;
use serde_json::json;

const LIMIT: Duration = Duration::from_secs(20);

fn rec(i: usize, body: &str, updated: &str) -> Rec {
    Rec::of(&json!({
        "id": format!("{i:026}"), "b": body, "c": "2024-01-31T12:00:00+00:00",
        "u": updated, "s": ["global"], "conf": "low",
        "rm": "2024-03-04T12:00:00+00:00", "rr": "",
    }))
}

#[test]
fn a_body_of_a_hundred_thousand_words() {
    let body: String = (0..100_000).map(|i| format!("w{i} ")).collect();
    let start = Instant::now();
    let cache = RawTokenCache::default();
    let recs = vec![rec(1, &body, "2024-03-01T12:00:00+00:00")];
    let hits = find_similar(&cache, &cps(&body), &recs, None, None).unwrap();
    assert_eq!(hits[0].similarity, 1.0);
    assert!(hits[0].snippet.len() <= 203);
    assert!(start.elapsed() < LIMIT);
}

#[test]
fn five_thousand_records_past_the_cache_bound() {
    let cache = RawTokenCache::default();
    let recs: Vec<Rec> = (0..6000)
        .map(|i| {
            rec(
                i,
                &format!("shared words here r{i} r{}", i + 1),
                "2024-03-01T12:00:00Z",
            )
        })
        .collect();
    let start = Instant::now();
    let hits = find_similar(&cache, &cps("shared words here r1"), &recs, None, None).unwrap();
    assert_eq!(hits.len(), 6000);
    assert_eq!(cache.keys().len(), 5000);
    assert!(start.elapsed() < LIMIT);
}

#[test]
fn compounds_of_many_parts() {
    let long = vec!["a"; 50_000].join("-");
    let dotted = vec!["x"; 50_000].join(".");
    let dashes = "-".repeat(100_000);
    let a: TokenSet = [long.clone(), dotted.clone(), dashes.clone()]
        .into_iter()
        .collect();
    let b: TokenSet = ["a".to_owned(), "x".to_owned()].into_iter().collect();
    let start = Instant::now();
    let r = pairwise_content_jaccard(&a, &b);
    assert!(r > 0.0 && r <= 1.0);
    let body = format!("{long} {dotted} {dashes}");
    let cache = RawTokenCache::default();
    let recs = vec![rec(1, "a x", "2024-03-01T12:00:00Z")];
    find_similar(&cache, &cps(&body), &recs, None, Some(0.0)).unwrap();
    assert!(start.elapsed() < LIMIT);
}

#[test]
fn a_long_tied_group_naive_and_aware() {
    let cache = RawTokenCache::default();
    let mut recs: Vec<Rec> = (0..3000)
        .map(|i| {
            rec(
                i,
                "same body text",
                &format!("2024-03-01T12:00:{:02}+00:00", i % 60),
            )
        })
        .collect();
    let hits = find_similar(&cache, &cps("same body text"), &recs, None, None).unwrap();
    assert_eq!(hits.len(), 3000);
    assert!(hits
        .windows(2)
        .all(|w| w[0].updated.instant_us() >= w[1].updated.instant_us()));
    recs[2999] = rec(2999, "same body text", "2024-03-01T12:00:00");
    assert_eq!(
        find_similar(&cache, &cps("same body text"), &recs, None, None),
        Err(DedupError::NaiveAware)
    );
    assert_eq!(
        find_similar_tombstones(&cache, &cps("same body text"), &recs, None, None)
            .unwrap()
            .len(),
        3000
    );
}

#[test]
fn integer_and_float_bounds() {
    for (s, n, m) in [
        (i64::MAX, i64::MAX, i64::MAX),
        (i64::MAX, 1, 1),
        (1, i64::MAX, i64::MAX),
        (i64::MIN, 1, 1),
        (1, i64::MIN, i64::MAX),
        (i64::MAX - 1, i64::MAX, i64::MAX),
    ] {
        let b = similarity_bound(s, n, m);
        assert!((0.0..=1.0).contains(&b), "{s} {n} {m}: {b}");
    }
    let cache = RawTokenCache::with_entries(i64::MIN);
    assert_eq!(cache.tokens(&cps("a")), Err(DedupError::EmptyCache));
    let cache = RawTokenCache::with_entries(i64::MAX);
    cache.tokens(&cps("a")).unwrap();
    let recs = vec![rec(1, "alpha", "2024-03-01T12:00:00Z")];
    for (hi, me) in [
        (f64::NAN, f64::NAN),
        (f64::INFINITY, f64::NEG_INFINITY),
        (-0.0, 0.0),
    ] {
        find_similar(&cache, &cps("alpha"), &recs, Some(hi), Some(me)).unwrap();
    }
}

#[test]
fn snippets_of_hostile_text() {
    let spaces: Vec<u32> = std::iter::repeat_n(0x3000, 1_000_000).collect();
    assert!(snippet_for(&spaces).is_empty());
    let surrogates: Vec<u32> = std::iter::repeat_n(0xD800, 500).collect();
    assert_eq!(snippet_for(&surrogates).len(), 203);
    let punct = ",".repeat(5000);
    assert_eq!(snippet_for(&cps(&punct)), cps("..."));
}
