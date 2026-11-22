//! Cases a plausible wrong port of the legacy questions, the shown
//! questions, the roots and the record counts (unit mlegacy) fails: the
//! remotes of a non-legacy checkout counted as the legacy project's, a
//! candidate with no remotes read as incompatible, the
//! rule's text compared after `bool`, the closed list in another order, a
//! shown line repeated or the resolution's own repeats dropped, the oldest
//! view taken as the root, a prefix without a separator read as below,
//! an `init` action matched without case, and a merge not followed when
//! the homes are counted.

mod common;

use std::collections::HashMap;

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::membership::Error;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::caller::Resolution;
use bettermemory_core::unicode::cps;
use common::membership_core::{model, Model};
use common::membership_legacy::named;
use common::membership_queries::{files_of, reader};
use common::membership_rows::lines;

fn model_of(tables: &str) -> Model {
    let all = lines("membership_legacy.jsonl");
    let named = named(&all[0]);
    let init = PyValue::Dict(vec![
        (
            PyValue::str("now"),
            PyValue::List(vec![PyValue::str("2026-03-04T05:06:07"), PyValue::int(0)]),
        ),
        (PyValue::str("session"), PyValue::None),
        (PyValue::str("given"), PyValue::Bool(false)),
    ]);
    model(
        &init,
        common::membership_core::tables_of(&named[tables]),
        None,
        false,
    )
}

fn ids(got: &[bettermemory_core::membership_records::records::Proposal]) -> Vec<String> {
    got.iter()
        .map(|p| p.id.iter().filter_map(|&c| char::from_u32(c)).collect())
        .collect()
}

#[test]
fn settle_closes_in_read_order_and_reads_legacy_remotes_only() {
    let mut m = model_of("settle");
    let got = m.settle_legacy_questions().expect("settles");
    assert_eq!(
        ids(&got),
        ["Q01", "Q04", "Q07", "Q13", "Q14", "Q15", "Q17", "Q18", "Q19", "Q22", "Q27"]
    );
    // Q27: C5's only remote r:z is held by L3's non-legacy checkout, so
    // closed. Q28: L2 has remotes, C1 none: compatible, left open.
    let read = ids(&got);
    assert!(read.contains(&"Q27".to_string()));
    assert!(!read.contains(&"Q28".to_string()));
    // Closed questions are returned as read, still open.
    assert!(got.iter().all(|p| p.state == cps("open")));
    // Idempotent: a second run finds nothing left to close.
    assert!(m.settle_legacy_questions().expect("settles").is_empty());
}

#[test]
fn declared_below_spells_a_lone_surrogate_as_a_replacement() {
    // A stated difference: Raised holds a Rust String, so the message
    // spells a lone surrogate as U+FFFD where the reference keeps it.
    let m = model_of("roots");
    let k = m.checkout(&cps("K1")).expect("a checkout");
    let dir: Vec<u32> = vec![0x2F, 0x78, 0xDCFF];
    match m.declared_below_in(Flavour::Posix, &cps("init"), Some(&dir), &k) {
        Err(Error::Raised(r)) => {
            assert_eq!(r.class, "ValueError");
            assert_eq!(
                r.message,
                "cannot tell where /x\u{fffd} stands in checkout K1 (seen at /w/one)"
            );
        }
        other => panic!("not a ValueError: {other:?}"),
    }
}

fn res(checkout: Option<&str>, lines: &[&str]) -> Resolution {
    Resolution {
        outcome: cps("silent"),
        project: None,
        checkout: checkout.map(cps),
        rule: cps("r"),
        proposal: None,
        candidates: Vec::new(),
        witnesses: Vec::new(),
        chain: Vec::new(),
        tenant: None,
        lines: lines.iter().map(|l| cps(l)).collect(),
        rows: Vec::new(),
        remint_token: false,
    }
}

#[test]
fn first_output_keeps_its_own_repeats_and_adds_no_shown_twice() {
    let mut m = model_of("shown");
    let files: HashMap<PyStr, PyValue> = files_of(&PyValue::Dict(vec![(
        PyValue::str("/w/one"),
        PyValue::Dict(vec![(PyValue::str("id"), PyValue::str("F1"))]),
    )]));
    let file = reader(&files);
    let quoted = cps("bettermemory: \"/w/one\" is new");
    let got = m
        .first_output(&res(Some("K1"), &["x", "x"]), &file)
        .expect("lines");
    assert_eq!(got[..2], [cps("x"), cps("x")]);
    assert_eq!(got.iter().filter(|l| **l == quoted).count(), 1);
    let got = m
        .first_output(&res(None, &["k", "k"]), &file)
        .expect("lines");
    assert_eq!(got, [cps("k"), cps("k")]);
    // A checkout of "" is not None: its questions are read.
    let got = m.first_output(&res(Some(""), &[]), &file).expect("lines");
    assert_eq!(got, [quoted]);
}

fn below(m: &Model, k: &str, dir: &str) -> Option<bool> {
    let k = m.checkout(&cps(k)).expect("a checkout");
    m.below_root_in(Flavour::Posix, &k, &cps(dir), None)
        .expect("an answer")
}

#[test]
fn below_root_reads_the_newest_view_here_with_a_separator() {
    let m = model_of("roots");
    assert_eq!(below(&m, "K1", "/w/onex"), None);
    assert_eq!(below(&m, "K1", "/w/one/sub"), Some(true));
    assert_eq!(below(&m, "K7", "/w/one"), Some(false));
    // K2's newest view here is /w/new; /w/old is older, /w/far elsewhere.
    assert_eq!(below(&m, "K2", "/w/new/x"), Some(true));
    assert_eq!(below(&m, "K2", "/w/old/x"), None);
    // Equal orders: the last of the stable sort.
    assert_eq!(below(&m, "K6", "/w/b/c"), Some(true));
    assert_eq!(below(&m, "K6", "/w/a"), None);
    // The root "/" holds everything absolute.
    assert_eq!(below(&m, "K4", "/w"), Some(true));
    // No view on this machine.
    assert_eq!(below(&m, "K3", "/w"), None);
    let k = m.checkout(&cps("K5")).expect("a checkout");
    let nt = |d: &str| {
        m.below_root_in(Flavour::Windows, &k, &cps(d), None)
            .expect("ok")
    };
    assert_eq!(nt("c:/w/one"), Some(false));
    assert_eq!(nt("C:\\w\\one\\sub"), Some(true));
}

#[test]
fn declared_below_matches_inits_exactly_and_raises_outside() {
    let m = model_of("roots");
    let k = m.checkout(&cps("K1")).expect("a checkout");
    let d = |action: &str, dir: Option<&str>| {
        let dir = dir.map(cps);
        m.declared_below_in(Flavour::Posix, &cps(action), dir.as_deref(), &k)
    };
    assert_eq!(d("init_new", Some("/w/one/sub")), Ok(true));
    assert_eq!(d("INIT", Some("/w/one/sub")), Ok(false));
    assert_eq!(d("init", None), Ok(false));
    match d("init", Some("/x")) {
        Err(Error::Raised(r)) => {
            assert_eq!(r.class, "ValueError");
            assert_eq!(
                r.message,
                "cannot tell where /x stands in checkout K1 (seen at /w/one)"
            );
        }
        other => panic!("not a ValueError: {other:?}"),
    }
}

#[test]
fn records_follow_merges_and_read_kind_exactly() {
    let mut m = model_of("counts");
    // P2 and P4 merge into P1: R1, R2 memories, R3 episode, R4 "Memory"
    // and R9 None counted as episodes.
    assert_eq!(m.record_counts(&cps("P4")), Ok((2, 3)));
    assert_eq!(m.record_counts(&cps("PX")), Ok((0, 0)));
    let got = m
        .records_by_project(&[cps("P4"), cps("P3"), cps("P2")])
        .expect("counts");
    assert_eq!(got, vec![(cps("P1"), (2, 3)), (cps("P3"), (1, 1))]);
}
