//! Cases a plausible wrong port of `verify::anchors` and `verify::count`
//! fails; each answer is the reference's (`verify.py` at v9).

#![cfg(unix)]

mod verify_count_script;
mod verify_weak_script;

use bettermemory_core::attestation_paths::{Kind, Paths};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_history::ReachableWalk;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::anchors::{commit_drift_anchor_paths, relative_citations};
use bettermemory_core::verify::count::{
    narrow_commit_drift, quiescent_drift_applicable, resolve_commit_drift_count, CountReads,
};
use bettermemory_core::verify::literal_lines::LiteralReader;
use bettermemory_core::verify::weak::Place;
use bettermemory_core::verify_basis::{BASIS_AUTHOR_DATE, BASIS_REACHABILITY};
use bettermemory_core::verify_candidates::scan::UserHome;
use serde_json::{json, Value};
use verify_count_script::Counted;
use verify_weak_script::literal;

fn anchors(body: &str, kind: Kind, vp: &[&str]) -> Vec<String> {
    let vp = Paths {
        kind,
        items: vp.iter().map(|p| cps(p)).collect(),
    };
    let home = cps("/home/u");
    let mut lookup = |_: &[u32]| UserHome::Unknown;
    commit_drift_anchor_paths(&cps(body), &vp, Some(&home), &mut lookup)
        .expect("an answer")
        .iter()
        .map(|p| {
            p.iter()
                .map(|&c| char::from_u32(c).expect("a char"))
                .collect()
        })
        .collect()
}

fn with<T>(s: Value, f: impl FnOnce(CountReads) -> T) -> T {
    let git = Counted::new(&s);
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
    f(c)
}

fn since() -> DateTime {
    DateTime::from_isoformat(&cps("2026-01-01T10:00:00+00:00")).expect("a stamp")
}

#[test]
fn the_cap_counts_only_new_relative_citations() {
    let body: String = (0..30).map(|i| format!("src/f{i}.py ")).collect();
    let att: Vec<String> = (0..5).map(|i| format!("src/f{i}.py")).collect();
    let att: Vec<&str> = att.iter().map(String::as_str).collect();
    assert_eq!(anchors(&body, Kind::Reference, &att).len(), 29);
    assert_eq!(anchors(&body, Kind::Plain, &[]).len(), 24);
}

#[test]
fn an_escaping_citation_is_skipped_and_not_counted() {
    let mut body = String::from("../a.py ");
    body.extend((0..30).map(|i| format!("g{i}.rs ")));
    let got = anchors(&body, Kind::Plain, &[]);
    assert_eq!(got.len(), 24);
    assert_eq!(got[0], "g0.rs");
}

#[test]
fn literal_and_reference_attestations_differ() {
    assert_eq!(
        anchors("", Kind::Literal, &["src/a.py:12"]),
        ["src/a.py:12"]
    );
    assert_eq!(anchors("", Kind::Plain, &["src/a.py:12"]), ["src/a.py"]);
    assert_eq!(
        anchors("", Kind::Reference, &["", "$HOME/x.py"]),
        ["~/x.py"]
    );
}

#[test]
fn the_citation_group_excludes_the_line_suffix_and_a_lookbehind_word() {
    let text = cps("see src/x.py:12 and /abs/y.py and ab-c.md.");
    let spans = relative_citations(&text).expect("an answer");
    let got: Vec<String> = spans
        .iter()
        .map(|&(a, b)| {
            text[a..b]
                .iter()
                .map(|&c| char::from_u32(c).expect("c"))
                .collect()
        })
        .collect();
    assert_eq!(got, ["src/x.py", "ab-c.md"]);
}

#[test]
fn an_equal_instant_is_not_counted() {
    let s = json!({"ts": ["2026-01-01T10:00:00+00:00", "2026-01-01T10:00:00.000001+00:00"]});
    let got = with(s, |c| {
        resolve_commit_drift_count(c, &since(), 7, &[cps("m.py")], &[], None)
    });
    assert_eq!(got, Ok(Some(1)));
}

#[test]
fn a_could_not_ask_keeps_the_conservative_count() {
    let got = with(json!({"ts": null}), |c| {
        resolve_commit_drift_count(c, &since(), 7, &[cps("m.py")], &[], None)
    });
    assert_eq!(got, Ok(Some(7)));
    let got = with(json!({"ts": []}), |c| {
        resolve_commit_drift_count(c, &since(), 7, &[cps("m.py")], &[], None)
    });
    assert_eq!(got, Ok(None));
}

#[test]
fn quiescent_could_not_ask_reads_applicable() {
    assert!(with(json!({"rp": null}), |c| quiescent_drift_applicable(
        c,
        &[cps("m.py")],
        &[]
    )));
    assert!(!with(json!({"rp": []}), |c| quiescent_drift_applicable(
        c,
        &[cps("m.py")],
        &[]
    )));
    assert!(with(json!({}), |c| quiescent_drift_applicable(
        c,
        &[cps("m.py")],
        &[]
    )));
}

#[test]
fn the_narrowing_keeps_the_basis_it_was_given() {
    let walk = ReachableWalk {
        anchor: cps("a"),
        head: cps("f"),
        commits: vec![cps("c1")],
        touched: [(cps("m.py"), [cps("c1")].into_iter().collect())]
            .into_iter()
            .collect(),
        listed: true,
        timed_out: false,
    };
    let anchors: Vec<PyStr> = vec![cps("m.py")];
    let got = with(json!({"rt": "/w"}), |c| {
        narrow_commit_drift(
            c,
            &since(),
            1,
            &anchors,
            &[],
            Some(&walk),
            BASIS_AUTHOR_DATE,
        )
    })
    .expect("an answer")
    .expect("applies");
    assert_eq!((got.count, got.basis.as_str()), (1, BASIS_AUTHOR_DATE));
    let got = with(json!({"hist": true}), |c| {
        narrow_commit_drift(c, &since(), 0, &anchors, &[], None, BASIS_REACHABILITY)
    })
    .expect("an answer")
    .expect("applies");
    assert_eq!((got.count, got.basis.as_str()), (0, BASIS_REACHABILITY));
}
