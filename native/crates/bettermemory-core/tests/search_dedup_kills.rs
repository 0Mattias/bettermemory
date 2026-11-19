//! Inputs on which a plausible wrong `search_dedup` answers otherwise than
//! the reference. Each expected value is the reference's (CPython 3.13.13).

mod common;

use bettermemory_core::search_dedup::{
    find_similar, find_similar_in, find_similar_tombstones, pairwise_content_jaccard,
    similarity_bound, snippet_for, DedupError, RawTokenCache, TokenSet, CONTAINMENT_CEILING,
};
use bettermemory_core::unicode::cps;
use common::dedup::{Fake, Rec};
use serde_json::json;

fn set(items: &[&str]) -> TokenSet {
    items.iter().map(|s| (*s).to_owned()).collect()
}

fn words(range: std::ops::Range<usize>) -> Vec<String> {
    range.map(|i| format!("w{i}")).collect()
}

fn owned(items: Vec<String>) -> TokenSet {
    items.into_iter().collect()
}

fn rec(i: u32, body: &str, updated: &str) -> Rec {
    Rec::of(&json!({
        "id": format!("{i:026}"), "b": body, "c": "2024-01-31T12:00:00+00:00",
        "u": updated, "s": ["global"], "conf": "high",
        "rm": "2024-03-04T12:00:00+00:00", "rr": "why",
    }))
}

const T0: &str = "2024-03-01T12:00:00+00:00";
const BODY: &str = "docker-compose up runs the prod stack on port 8080";

#[test]
fn a_compound_both_sides_share_is_not_expanded() {
    // Symmetric expansion (`_content_token_set`) scores 0.6 here.
    let r = pairwise_content_jaccard(
        &set(&["docker-compose", "prod"]),
        &set(&["docker-compose", "dev"]),
    );
    assert_eq!(r, 1.0 / 3.0);
}

#[test]
fn a_compound_one_side_lacks_is_expanded_without_stopword_parts() {
    let r = pairwise_content_jaccard(
        &set(&["python-frontmatter"]),
        &set(&["python", "frontmatter"]),
    );
    assert_eq!(r, 2.0 / 3.0);
    // Keeping the part "of" would score 1/3.
    assert_eq!(
        pairwise_content_jaccard(&set(&["of-cake"]), &set(&["cake"])),
        0.5
    );
}

#[test]
fn containment_needs_eight_tokens_and_is_capped() {
    let seven = pairwise_content_jaccard(
        &owned(words(0..7)),
        &owned([words(0..7), words(10..30)].concat()),
    );
    assert_eq!(seven, 7.0 / 27.0);
    let eight = pairwise_content_jaccard(
        &owned(words(0..8)),
        &owned([words(0..8), words(10..30)].concat()),
    );
    assert_eq!(eight, CONTAINMENT_CEILING);
    assert_eq!(CONTAINMENT_CEILING.to_bits(), 0x3fe2666666666666);
}

#[test]
fn the_bound_past_the_sets() {
    assert_eq!(similarity_bound(10, 3, 3), 1.0);
    assert_eq!(similarity_bound(8, 8, 30), CONTAINMENT_CEILING);
    assert_eq!(similarity_bound(0, 3, 3), 0.0);
    assert_eq!(similarity_bound(i64::MAX, i64::MAX, i64::MAX), 1.0);
}

#[test]
fn an_empty_tombstone_list_tokenizes_nothing() {
    let cache = RawTokenCache::default();
    let none: Vec<Rec> = Vec::new();
    find_similar_tombstones(&cache, &cps(BODY), &none, None, None).unwrap();
    assert!(cache.keys().is_empty());
    find_similar(&cache, &cps(BODY), &none, None, None).unwrap();
    assert_eq!(cache.keys(), vec![cps(BODY)]);
}

#[test]
fn ties_keep_input_order_after_rounding() {
    // 12/119 and 13/129 round alike to 0.1008: the reference sorts the
    // rounded value, so input order stands (1, 2, 3), not (1, 3, 2).
    let new: Vec<String> = words(0..100);
    let a = [words(0..12), (0..19).map(|j| format!("xa{j}")).collect()].concat();
    let b = [words(0..13), (0..29).map(|j| format!("xb{j}")).collect()].concat();
    let recs = vec![
        rec(1, &a.join(" "), T0),
        rec(2, &b.join(" "), T0),
        rec(3, &a.join(" "), T0),
    ];
    let cache = RawTokenCache::default();
    let hits = find_similar(&cache, &cps(&new.join(" ")), &recs, None, Some(0.0)).unwrap();
    let ids: Vec<u32> = hits.iter().map(|h| h.id[25] - 0x30).collect();
    assert_eq!(ids, vec![1, 2, 3]);
    assert!(hits.iter().all(|h| h.similarity == 0.1008));
}

#[test]
fn equal_scores_order_by_the_newer_update_then_input() {
    let recs = vec![
        rec(1, BODY, "2024-03-01T12:00:00+00:00"),
        rec(2, BODY, "2024-03-01T13:00:00+02:00"),
        rec(3, BODY, "2024-03-01T12:30:00+00:00"),
    ];
    let cache = RawTokenCache::default();
    let hits = find_similar(&cache, &cps(BODY), &recs, None, None).unwrap();
    let ids: Vec<u32> = hits.iter().map(|h| h.id[25] - 0x30).collect();
    // 13:00+02:00 is 11:00 UTC: the oldest. Wall-clock order would put 2 first.
    assert_eq!(ids, vec![3, 1, 2]);
    assert_eq!(hits[0].relevance, "high");
}

#[test]
fn naive_against_aware_raises_only_inside_a_tie() {
    let cache = RawTokenCache::default();
    let tied = vec![rec(1, BODY, "2024-03-01T12:00:00"), rec(2, BODY, T0)];
    assert_eq!(
        find_similar(&cache, &cps(BODY), &tied, None, None),
        Err(DedupError::NaiveAware)
    );
    let apart = vec![
        rec(1, BODY, "2024-03-01T12:00:00"),
        rec(2, "docker-compose up runs the dev stack on port 8080", T0),
    ];
    assert_eq!(
        find_similar(&cache, &cps(BODY), &apart, None, None)
            .unwrap()
            .len(),
        2
    );
    // The tombstone pass sorts on `removed`, aware in both.
    assert_eq!(
        find_similar_tombstones(&cache, &cps(BODY), &tied, None, None)
            .unwrap()
            .len(),
        2
    );
}

#[test]
fn a_threshold_is_reached_at_equality() {
    let cache = RawTokenCache::default();
    let recs = vec![rec(1, BODY, T0)];
    let hits = find_similar(&cache, &cps(BODY), &recs, Some(1.0), Some(1.0)).unwrap();
    assert_eq!(hits[0].relevance, "high");
    // A zero score is dropped whatever the medium threshold.
    let other = vec![rec(2, "unrelated words entirely", T0)];
    assert!(find_similar(&cache, &cps(BODY), &other, None, Some(-1.0))
        .unwrap()
        .is_empty());
}

#[test]
fn the_cache_touches_on_a_hit_and_raises_under_a_negative_bound() {
    let cache = RawTokenCache::with_entries(2);
    for t in ["a b", "c", "a b", "d"] {
        cache.tokens(&cps(t)).unwrap();
    }
    assert_eq!(cache.keys(), vec![cps("a b"), cps("d")]);
    let zero = RawTokenCache::with_entries(0);
    assert_eq!(*zero.tokens(&cps("alpha")).unwrap(), set(&["alpha"]));
    assert!(zero.keys().is_empty());
    let negative = RawTokenCache::with_entries(-1);
    assert_eq!(negative.tokens(&cps("alpha")), Err(DedupError::EmptyCache));
    assert!(negative.keys().is_empty());
}

#[test]
fn a_snippet_backs_off_to_a_boundary_at_160_not_159() {
    let at = format!("{} {}", "y".repeat(160), "z".repeat(60));
    assert_eq!(
        snippet_for(&cps(&at)),
        cps(&format!("{}...", "y".repeat(160)))
    );
    let before = format!("{} {}", "y".repeat(159), "z".repeat(60));
    let cut: String = before.chars().take(200).collect();
    assert_eq!(snippet_for(&cps(&before)), cps(&format!("{cut}...")));
    // A carriage return is no boundary.
    let cr = format!("{}\r{}", "y".repeat(165), "z".repeat(60));
    let cut: String = cr.chars().take(200).collect();
    assert_eq!(snippet_for(&cps(&cr)), cps(&format!("{cut}...")));
}

#[test]
fn the_digest_scores_each_id_once_in_first_place_with_its_last_tokens() {
    let toks = [
        "8080",
        "docker-compos",
        "port",
        "prod",
        "run",
        "stack",
        "up",
    ];
    let more = [&toks[..], &["extra"]].concat();
    let id = |i: u32| format!("{i:026}");
    let rec_json =
        |i: u32| json!({"id": id(i), "b": BODY, "c": T0, "u": T0, "s": ["global"], "conf": "high"});
    let case = json!({
        "docs": [[id(2), toks], [id(1), toks], [id(2), more]],
        "memo": "keep", "light": [rec_json(1), rec_json(2)], "all": [], "tomb": [],
    });
    let cache = RawTokenCache::default();
    let mut store = Fake::new(&case);
    let hits = find_similar_in(&cache, &mut store, &cps(BODY)).unwrap();
    // A dict keeps a key's first place and its last value.
    assert_eq!(store.log[2], json!(["lr", [id(2), id(1)], false]));
    let got: Vec<(u32, f64)> = hits
        .iter()
        .map(|h| (h.id[25] - 0x30, h.similarity))
        .collect();
    assert_eq!(got, vec![(1, 1.0), (2, 0.875)]);
}

#[test]
fn the_bound_with_no_room_past_the_shared_tokens() {
    // n + m - shared == 0 takes no ratio; the reference answers 1.0.
    assert_eq!(similarity_bound(6, 3, 3), 1.0);
}

#[test]
fn the_digest_keeps_a_doc_at_exactly_the_medium_floor() {
    let id = format!("{:026}", 1);
    let doc = ["alpha", "bravo", "charlie", "delta", "echo"];
    let rec_json = json!({"id": id, "b": "alpha bravo charlie delta echo", "c": T0, "u": T0,
        "s": ["global"], "conf": "high"});
    let case = json!({
        "docs": [[id, doc]], "memo": "keep", "light": [rec_json], "all": [], "tomb": [],
    });
    let cache = RawTokenCache::default();
    let mut store = Fake::new(&case);
    let hits = find_similar_in(&cache, &mut store, &cps("alpha bravo")).unwrap();
    let got: Vec<f64> = hits.iter().map(|h| h.similarity).collect();
    assert_eq!(got, vec![0.4]);
}

#[test]
fn concurrent_callers_leave_one_key_with_equal_sets() {
    let cache = std::sync::Arc::new(RawTokenCache::default());
    let handles: Vec<_> = (0..8)
        .map(|_| {
            let cache = cache.clone();
            std::thread::spawn(move || cache.tokens(&cps("a b")).unwrap())
        })
        .collect();
    let sets: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    assert_eq!(cache.keys(), vec![cps("a b")]);
    assert!(sets.iter().all(|s| **s == *sets[0]));
}

#[test]
fn a_late_store_of_a_present_key_keeps_its_place() {
    // A caller that tokenized "a b" while another inserted it, then "c d"
    // went in: `d[key] = value` on a present key leaves the order a b, c d.
    let cache = RawTokenCache::default();
    cache.tokens(&cps("a b")).unwrap();
    cache.tokens(&cps("c d")).unwrap();
    let late = std::sync::Arc::new(set(&["a", "b"]));
    let held = cache.store(&cps("a b"), late).unwrap();
    assert_eq!(*held, set(&["a", "b"]));
    assert_eq!(cache.keys(), vec![cps("a b"), cps("c d")]);
}
