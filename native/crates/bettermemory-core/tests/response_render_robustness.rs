//! Hostile input to `response_render`: long lists, deep values, long
//! paths and integers past any machine width. No panic in debug or
//! release, and time that grows with the input as the reference's does.

mod timing;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::Memory;
use bettermemory_core::response_render::{
    grouped_links, path_states, render_hit, render_show_sep, ClaimRejected, ParsedClaim,
    PathDriftLists,
};
use bettermemory_core::unicode::cps;

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn dict(items: Vec<(&str, PyValue)>) -> Vec<(PyValue, PyValue)> {
    items.into_iter().map(|(k, v)| (s(k), v)).collect()
}

fn hit(extra: Vec<(&str, PyValue)>) -> Vec<(PyValue, PyValue)> {
    let mut r = dict(vec![
        ("id", s("a")),
        ("snippet", s("s")),
        ("relevance", PyValue::Float(0.5)),
        ("staleness_verdict", s("fresh")),
    ]);
    r.extend(dict(extra));
    r
}

fn memory(f: Vec<(&str, PyValue)>) -> Memory {
    let mut m = dict(vec![
        ("id", s("01ARZ3NDEKTSV4RRFFQ69G5FAV")),
        ("created", s("2026-01-02T03:04:05Z")),
        ("updated", s("2026-01-02T03:04:05Z")),
        ("scopes", PyValue::List(vec![s("global")])),
        ("confidence", s("medium")),
        ("source", s("explicit-statement")),
        ("body", s("b")),
    ]);
    m.extend(dict(f));
    Memory::from_payload(&PyValue::Dict(m)).unwrap()
}

fn no_claims(_: &[u32]) -> ParsedClaim {
    Err(ClaimRejected)
}

fn nested(depth: usize) -> PyValue {
    let mut v = PyValue::None;
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

fn paths(n: usize, tag: &str) -> Vec<PyStr> {
    (0..n).map(|i| cps(&format!("/r/{tag}{i}"))).collect()
}

/// The reference raises `RecursionError` comparing two distinct lists
/// this deep as `created` and `updated`; the port compares iteratively
/// and answers. The README states the difference.
#[test]
fn values_nested_100_000_deep_pass_through() {
    let deep = nested(100_000);
    let out = render_hit(&hit(vec![("body", deep.clone()), ("held", deep.clone())])).unwrap();
    assert_eq!(out.len(), 7);
    let state = dict(vec![
        ("id", deep.clone()),
        ("staleness_verdict", s("fresh")),
        ("use_token", deep.clone()),
        ("scopes", deep.clone()),
        ("body", deep.clone()),
        ("created", deep.clone()),
        ("updated", deep.clone()),
        ("verification", PyValue::Dict(vec![])),
        ("actor", PyValue::Dict(dict(vec![("a", deep.clone())]))),
        ("standing_question", deep),
    ]);
    let out = render_show_sep(
        &state,
        &memory(vec![]),
        &PathDriftLists::default(),
        &mut no_claims,
        None,
    )
    .unwrap();
    assert!(out.len() >= 9);
}

#[test]
fn a_corroboration_count_past_any_width_is_kept_as_text() {
    let big = "9".repeat(400);
    let m = memory(vec![("corroborations", PyValue::Int(big.clone()))]);
    let state = dict(vec![
        ("id", s("a")),
        ("staleness_verdict", s("fresh")),
        ("use_token", s("t")),
        ("scopes", s("x")),
        ("body", s("b")),
        ("created", s("c")),
        ("updated", s("c")),
        ("verification", PyValue::Dict(vec![])),
    ]);
    let out =
        render_show_sep(&state, &m, &PathDriftLists::default(), &mut no_claims, None).unwrap();
    assert!(out.contains(&(s("corroborations"), PyValue::Int(big))));
}

#[test]
fn a_path_of_a_million_characters_is_matched() {
    let long = "a/".repeat(500_000);
    let d = PathDriftLists {
        checked: vec![cps(&format!("/r/{long}x"))],
        ..Default::default()
    };
    let m = memory(vec![(
        "verified_paths",
        PyValue::List(vec![s(&format!("./{long}x"))]),
    )]);
    let (p, _) = path_states(&m, &d, &mut no_claims, Some('\\')).unwrap();
    assert_eq!(p.len(), 1);
}

#[test]
fn the_drift_lists_grow_linearly() {
    let run = |n: usize| {
        let d = PathDriftLists {
            missing: paths(n, "m"),
            claim_anchored_missing: paths(n, "m"),
            verified: paths(n, "v"),
            unconfirmed: paths(n, "u"),
            unconfirmed_reasons: paths(n, "why"),
            checked: paths(n, "c"),
            ..Default::default()
        };
        let m = memory(vec![]);
        move || path_states(&m, &d, &mut no_claims, None).unwrap()
    };
    let mut small = run(5_000);
    let mut large = run(50_000);
    timing::grows_within(
        "path_states: 5,000 and 50,000 paths a list",
        timing::TENFOLD,
        &mut || timing::once(&mut small),
        &mut || timing::once(&mut large),
    );
}

#[test]
fn links_in_distinct_groups_grow_linearly() {
    let run = |n: usize| {
        let entries = PyValue::List(
            (0..n)
                .map(|i| {
                    PyValue::Dict(dict(vec![
                        ("type", s(&format!("t{i}"))),
                        ("target_id", s("x")),
                    ]))
                })
                .collect(),
        );
        move || grouped_links(&entries, "target_id").unwrap()
    };
    let mut small = run(10_000);
    let mut large = run(100_000);
    timing::grows_within(
        "grouped_links: 10,000 and 100,000 distinct groups",
        timing::TENFOLD,
        &mut || timing::once(&mut small),
        &mut || timing::once(&mut large),
    );
}

#[test]
fn links_in_few_groups_grow_linearly() {
    let run = |n: usize| {
        let entries = PyValue::List(
            (0..n)
                .map(|i| {
                    PyValue::Dict(dict(vec![
                        ("type", s(["supersedes", "relates"][i % 2])),
                        ("target_id", s(&format!("t{i}"))),
                    ]))
                })
                .collect(),
        );
        move || grouped_links(&entries, "target_id").unwrap()
    };
    let mut small = run(10_000);
    let mut large = run(100_000);
    timing::grows_within(
        "grouped_links: 10,000 and 100,000 links",
        timing::TENFOLD,
        &mut || timing::once(&mut small),
        &mut || timing::once(&mut large),
    );
}

#[test]
fn many_contradictions_grow_linearly() {
    let run = |n: usize| {
        let ids = PyValue::List(
            (0..n)
                .map(|i| PyValue::Dict(dict(vec![("id", s(&format!("c{i}")))])))
                .collect(),
        );
        let row = hit(vec![("contradicts", ids.clone()), ("superseded_by", ids)]);
        move || render_hit(&row).unwrap()
    };
    let mut small = run(10_000);
    let mut large = run(100_000);
    timing::grows_within(
        "render_hit: 10,000 and 100,000 ids",
        timing::TENFOLD,
        &mut || timing::once(&mut small),
        &mut || timing::once(&mut large),
    );
}
