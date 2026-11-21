//! Cases a plausible wrong port of membership-3 and
//! `location.continuity` fails: a reader asked where the reference does
//! not ask it, a token test turned round, the first of equal orders lost,
//! the walk's bound off by one, the exclusion read before the merge.

mod common;

use std::collections::{BTreeMap, BTreeSet};
use std::rc::Rc;

use bettermemory_core::capture::location::Location;
use bettermemory_core::checkout_token;
use bettermemory_core::fsdata::Flavour;
use bettermemory_core::location_continuity::{continuity, Readers};
use bettermemory_core::membership::Result2;
use bettermemory_core::membership_records::records::{Checkout, HistoryReader, View};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use common::membership_views::{location, History, Run};

type R = Result<bool, ()>;

/// A full commit id: `_witness_query` keeps no other.
const C1: &str = "cccccccccccccccccccccccccccccccccccccccc";

fn run(
    token: Option<&str>,
    here: Option<&str>,
    superseded: bool,
    remint: bool,
) -> (bool, &'static str, usize) {
    let mut asked = 0;
    let mut holds = |_: &[u32]| -> R { Ok(false) };
    let mut reminted = || -> R {
        asked += 1;
        Ok(remint)
    };
    let (t, h) = (token.map(cps), here.map(cps));
    let (ok, how) = continuity::<(i32, i32), ()>(
        t.as_deref(),
        None,
        &[],
        h.as_deref(),
        None,
        &[],
        superseded,
        Readers {
            holds: &mut holds,
            confirms: None,
            reminted: Some(&mut reminted),
        },
    )
    .unwrap();
    (ok, how, asked)
}

#[test]
fn a_token_found_nowhere_is_never_a_remint() {
    assert_eq!(run(Some("A"), None, false, true), (false, "token", 0));
    assert_eq!(run(Some("A"), Some(""), false, true), (true, "remint", 1));
    assert_eq!(run(Some("A"), Some("A"), false, true), (true, "token", 0));
    assert_eq!(
        run(Some(""), Some("B"), false, true),
        (false, "nothing_observable", 0)
    );
}

#[test]
fn a_superseded_token_decides_only_where_none_is_found() {
    assert_eq!(run(Some("A"), Some(""), true, false), (false, "token", 1));
    assert_eq!(
        run(Some("A"), Some("B"), true, false),
        (false, "nothing_observable", 1)
    );
    assert_eq!(run(Some("A"), Some("A"), true, false), (true, "token", 0));
}

#[test]
fn init_stamps_meet_on_any_equal_item() {
    let mut holds = |_: &[u32]| -> R { panic!("not asked") };
    let got = continuity::<(i32, i32), ()>(
        None,
        None,
        &[(1, 2), (3, 4)],
        None,
        None,
        &[(5, 6), (3, 4)],
        false,
        Readers {
            holds: &mut holds,
            confirms: None,
            reminted: None,
        },
    );
    assert_eq!(got, Ok((true, "init_stamps")));
}

fn tables(rows: Vec<(&str, Vec<PyValue>)>) -> BTreeMap<String, Vec<PyValue>> {
    rows.into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}

fn row(items: &[(&str, PyValue)]) -> PyValue {
    PyValue::Dict(
        items
            .iter()
            .map(|(k, v)| (PyValue::str(k), v.clone()))
            .collect(),
    )
}

fn witness(project: &str, commit: &str, arrival: i128) -> PyValue {
    row(&[
        ("project", PyValue::str(project)),
        ("commit_id", PyValue::str(commit)),
        ("arrival", PyValue::int(arrival)),
        ("fingerprint", PyValue::str("f")),
    ])
}

fn project(id: &str, merged: Option<&str>) -> PyValue {
    row(&[
        ("id", PyValue::str(id)),
        ("name", PyValue::str(id)),
        ("merged_into", merged.map_or(PyValue::None, PyValue::str)),
        ("provisional", PyValue::None),
        ("parent", PyValue::None),
        ("tenant", PyValue::None),
        ("folder", PyValue::int(0)),
        ("boundary", PyValue::int(0)),
        ("minted", PyValue::str("t")),
    ])
}

fn spec(items: &[(&str, PyValue)]) -> PyValue {
    row(items)
}

#[test]
fn the_exclusion_is_of_the_canonical_project() {
    let t = tables(vec![
        (
            "projects",
            vec![project("P1", None), project("P2", Some("P1"))],
        ),
        ("project_witnesses", vec![witness("P2", C1, 1)]),
    ]);
    let mut r = Run::new(t, &PyValue::Dict(Vec::new()));
    let h = History::of(
        &spec(&[("holds", PyValue::List(vec![PyValue::str(C1)]))]),
        Rc::default(),
    )
    .unwrap();
    let loc = location(&spec(&[("head", PyValue::str("h"))]));
    let held =
        r.m.held(&loc, Some(&h as &dyn HistoryReader), Some(&cps("P2")))
            .unwrap();
    assert_eq!(held.keys().cloned().collect::<Vec<_>>(), vec![cps("P1")]);
    let held =
        r.m.held(&loc, Some(&h as &dyn HistoryReader), Some(&cps("P1")))
            .unwrap();
    assert!(held.is_empty());
}

#[test]
fn the_walk_reads_exactly_its_bound() {
    let t = tables(vec![("project_witnesses", vec![witness("P1", C1, 1)])]);
    let mut r = Run::new(t, &PyValue::Dict(Vec::new()));
    let fp: Vec<(PyValue, PyValue)> = (0..600)
        .map(|i| (PyValue::str(&format!("n{i}")), PyValue::str("f")))
        .collect();
    let parents: Vec<(PyValue, PyValue)> = (0..600)
        .map(|i| {
            (
                PyValue::str(&format!("n{i}")),
                PyValue::List(vec![PyValue::str(&format!("n{}", i + 1))]),
            )
        })
        .collect();
    let h = History::of(
        &spec(&[
            ("fp", PyValue::Dict(fp)),
            ("parents", PyValue::Dict(parents)),
        ]),
        Rc::default(),
    )
    .unwrap();
    let loc = location(&spec(&[("head", PyValue::str("n0"))]));
    let got =
        r.m.rewritten(&loc, Some(&h as &dyn HistoryReader), None)
            .unwrap();
    assert_eq!(got.get(&cps("P1")), Some(&512));
}

#[test]
fn a_second_set_of_witnesses_is_left_to_the_traversal() {
    let t = tables(vec![]);
    let mut r = Run::new(t, &PyValue::Dict(Vec::new()));
    let h = History::of(&spec(&[("git", PyValue::Bool(true))]), Rc::default()).unwrap();
    let mut asked: BTreeMap<Vec<PyStr>, Option<BTreeSet<PyStr>>> = BTreeMap::new();
    asked.insert(vec![cps("a"), cps("c")], None);
    r.m.ancestry = Some(asked);
    assert_eq!(r.m.independent(Some(&h), &[cps("a"), cps("b")]), Ok(None));
    r.m.ancestry = Some(BTreeMap::new());
    let need =
        r.m.independent(Some(&h), &[cps("a"), cps("b")])
            .unwrap_err();
    assert_eq!(need.commits, vec![cps("a"), cps("b")]);
    assert_eq!(r.m.independent(None, &[cps("a"), cps("b")]), Ok(None));
    assert_eq!(
        r.m.independent(None, &[cps("a")]),
        Ok(Some([cps("a")].into_iter().collect()))
    );
}

fn checkout_run(live: &[(&str, &str)]) -> Run {
    let t = tables(vec![
        ("projects", vec![project("P1", None)]),
        (
            "checkouts",
            vec![common::membership_views::checkout_row("K1")],
        ),
    ]);
    let live = PyValue::Dict(
        live.iter()
            .map(|(p, s)| (PyValue::str(p), PyValue::str(s)))
            .collect(),
    );
    Run::new(t, &live)
}

fn view(path: &str) -> View {
    View {
        checkout: cps("K1"),
        machine: cps("M1"),
        path: cps(path),
        first_seen: cps("t"),
        last_seen: cps("t"),
        order: 1,
    }
}

fn number(device: &str, inode: &str) -> PyValue {
    PyValue::List(vec![
        PyValue::str(device),
        PyValue::str(inode),
        PyValue::None,
    ])
}

/// Rule S needs a linked worktree's store: a location whose object is its
/// own common store is not one, and without H nothing confirms it.
#[test]
fn a_store_that_is_its_own_object_is_no_linked_worktree() {
    let mut r = checkout_run(&[]);
    let mut k = r.m.checkout(&cps("K1")).unwrap();
    let loc = location(&spec(&[
        ("object_key", number("1", "100")),
        ("common_object_key", number("1", "100")),
    ]));
    k.common_object_key = loc.common_object_key.clone();
    assert_eq!(r.m.confirms(&k, &loc, None), Ok(false));
    let linked = location(&spec(&[
        ("object_key", number("1", "200")),
        ("common_object_key", number("1", "100")),
    ]));
    assert_eq!(r.m.confirms(&k, &linked, None), Ok(true));
}

/// `succeeds(here, k.lineage) or _shared_remint(...)`: the shared re-mint
/// is never asked where the token found is the checkout's own re-mint.
#[test]
fn a_remint_the_token_proves_asks_no_other() {
    let mut r = checkout_run(&[]);
    let mut k = r.m.checkout(&cps("K1")).unwrap();
    let need = checkout_token::random_bytes_needed(26).unwrap();
    k.token = Some(cps(&checkout_token::mint(&vec![7u8; need]).unwrap()));
    let lineage = k.lineage().expect("a lineage");
    let nonce = checkout_token::random_bytes_needed(13).unwrap();
    let next = checkout_token::successor(&lineage, &vec![9u8; nonce]).unwrap();
    assert!(checkout_token::succeeds(&PyValue::str(&next), Some(&lineage)).unwrap());
    let loc = location(&spec(&[("token", PyValue::str(&next))]));
    let mut remint = |_: &Checkout, _: &Location, _: Option<&PyStr>| -> Result2<bool, _> {
        panic!("the shared re-mint is not asked")
    };
    assert_eq!(
        r.m.continuity(&k, &loc, None, &mut remint),
        Ok((true, "remint"))
    );
}

fn bare(r: &mut Run, git_dir: Option<&str>) -> Checkout {
    let mut k = r.m.checkout(&cps("K1")).unwrap();
    k.kind = cps("bare");
    k.git_dir = git_dir.map(cps);
    k.views = vec![view("/w/bare")];
    k
}

fn worktree_of_bare() -> Location {
    let mut loc = location(&spec(&[
        ("kind", PyValue::str("linked")),
        ("root", PyValue::str("/w/x")),
        ("git_dir", PyValue::str("/w/bare/worktrees/x")),
    ]));
    loc.common_object_key = loc.object_key.clone();
    loc
}

/// A bare checkout's live view is its common directory: it decides where
/// no git directory is recorded, or an old one is, under either flavour.
#[test]
fn a_bare_checkout_is_named_by_its_live_view() {
    for flavour in [Flavour::Posix, Flavour::Windows] {
        for recorded in [None, Some("/w/old")] {
            let mut r = checkout_run(&[("/w/bare", "live")]);
            let mut k = bare(&mut r, recorded);
            let loc = worktree_of_bare();
            k.common_object_key = loc.object_key.clone();
            assert!(
                r.m.by_pointer_in(flavour, &k, &loc),
                "{flavour:?} {recorded:?}"
            );
            let mut r = checkout_run(&[]);
            let mut k = bare(&mut r, recorded);
            k.common_object_key = loc.object_key.clone();
            assert!(!r.m.by_pointer_in(flavour, &k, &loc), "{flavour:?} gone");
        }
    }
}

/// Continuity and the pointer read inside a span answer as outside it,
/// and the span reads each view's liveness once.
#[test]
fn continuity_and_the_pointer_answer_alike_in_a_span() {
    let loc = worktree_of_bare();
    let outside = {
        let mut r = checkout_run(&[("/w/bare", "live")]);
        let mut k = bare(&mut r, None);
        k.common_object_key = loc.object_key.clone();
        let mut remint = |_: &Checkout, _: &Location, _: Option<&PyStr>| Ok(true);
        let a = r.m.continuity(&k, &loc, None, &mut remint).unwrap();
        let b = r.m.by_pointer_in(Flavour::Posix, &k, &loc);
        (a, b, r.m.by_pointer_in(Flavour::Posix, &k, &loc))
    };
    let mut r = checkout_run(&[("/w/bare", "live")]);
    let mut k = bare(&mut r, None);
    k.common_object_key = loc.object_key.clone();
    let inside = r.m.liveness_held(|m| {
        let mut remint = |_: &Checkout, _: &Location, _: Option<&PyStr>| Ok(true);
        let a = m.continuity(&k, &loc, None, &mut remint).unwrap();
        let b = m.by_pointer_in(Flavour::Posix, &k, &loc);
        (a, b, m.by_pointer_in(Flavour::Posix, &k, &loc))
    });
    assert_eq!(inside, outside);
    assert_eq!(r.lv.borrow().len(), 1);
    assert!(r.m.live_held().is_none());
}

/// The reference clears the span in a `finally`: a caught unwind leaves
/// no span behind, and liveness is read again afterwards.
#[test]
fn an_unwind_out_of_a_span_clears_it() {
    let mut r = checkout_run(&[("/w/bare", "live")]);
    let k = bare(&mut r, None);
    let got = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        r.m.liveness_held(|m| {
            m.live_view(&k);
            panic!("out of the span")
        })
    }));
    assert!(got.is_err());
    assert!(r.m.live_held().is_none());
    r.m.live_view(&k);
    assert_eq!(r.lv.borrow().len(), 2);
}
