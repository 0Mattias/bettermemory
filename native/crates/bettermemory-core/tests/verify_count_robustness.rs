//! Hostile inputs for the claim anchors and the count: a body past the
//! scan bound, a pathological citation run, many attestations, extreme
//! dates; no panic, time linear in the body.

#![cfg(unix)]

mod timing;
mod verify_count_script;
mod verify_weak_script;

use bettermemory_core::attestation_paths::{Kind, Paths};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::anchors::{commit_drift_anchor_paths, relative_citations};
use bettermemory_core::verify::count::{resolve_commit_drift_count, CountReads};
use bettermemory_core::verify::literal_lines::LiteralReader;
use bettermemory_core::verify::weak::Place;
use bettermemory_core::verify_candidates::scan::UserHome;
use serde_json::json;
use verify_count_script::Counted;
use verify_weak_script::literal;

fn run(body: &[u32], items: Vec<Vec<u32>>) -> usize {
    let vp = Paths {
        kind: Kind::Reference,
        items,
    };
    let home = cps("/home/u");
    let mut lookup = |_: &[u32]| UserHome::Unknown;
    commit_drift_anchor_paths(body, &vp, Some(&home), &mut lookup)
        .expect("an answer")
        .len()
}

#[test]
fn a_body_past_the_scan_bound_and_many_attestations() {
    let body = cps(&"a/b/c/d/e/f.py ".repeat(200_000));
    let items: Vec<Vec<u32>> = (0..20_000).map(|i| cps(&format!("p{i}.rs"))).collect();
    assert_eq!(run(&body, items), 20_001);
}

#[test]
fn pathological_runs_for_the_citation_pattern() {
    for unit in ["a.", "a/", "a-", ".a", "a.b/", "\u{e9}", "a.py.", "/x.py"] {
        let text = cps(&unit.repeat(40_000));
        relative_citations(&text).expect("an answer");
        run(&text, Vec::new());
    }
}

#[test]
fn time_is_linear_in_the_body() {
    let body = |n: usize| cps(&"see dir/sub/file_name.py:12 and x.rs; ".repeat(n));
    let (small, large) = (body(80), body(800));
    let time = |b: &[u32]| timing::once(|| run(b, Vec::new()));
    timing::grows_within("anchors", 30, &mut || time(&small), &mut || time(&large));
}

#[test]
fn extreme_dates_and_counts() {
    let git = Counted::new(&json!({"ts": [
        "0001-01-01T00:00:00+00:00",
        "9999-12-31T23:59:59.999999+00:00",
        "9999-12-31T23:59:59.999999-23:59"
    ]}));
    let lit = LiteralReader::default();
    let cwd = cps("/w");
    let c = CountReads {
        git: &git,
        at: Place {
            cwd: &cwd,
            toplevel: None,
        },
        literal_pathspecs: &literal,
        lit: &lit,
    };
    for (stamp, want) in [
        ("0001-01-01T00:00:00-23:59", 2),
        ("9999-12-31T23:59:59.999999+00:00", 1),
        ("0001-01-01T00:00:00", 2),
    ] {
        let since = DateTime::from_isoformat(&cps(stamp)).expect("a stamp");
        let got = resolve_commit_drift_count(c, &since, i64::MAX, &[cps("m")], &[], None);
        assert_eq!(got, Ok(Some(want)), "{stamp}");
    }
}
