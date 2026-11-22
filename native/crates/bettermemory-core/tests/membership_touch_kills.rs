//! Cases a plausible wrong sighting or token unit (membership-4a) fails:
//! the one-day refresh bound taken as strict, a sighting that cannot be
//! read taken as fresh, an older view left alone, a legacy-live view not
//! counted as live, the previous view taken as the first, `_give_token`
//! given `token_written`'s guard against a `superseded` status, an event
//! for a write left to another machine, and `_foreign` read as any
//! status, and a raise of the writer swallowed.

mod common;

use bettermemory_core::membership::touch::{foreign, CONTENDED, OTHER_MACHINE_SKIP};
use bettermemory_core::membership::{Error, TokenWriter};
use bettermemory_core::membership_lines::Raised;
use bettermemory_core::membership_records::records::Checkout;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use common::membership_core::{tables_of, Model};
use common::membership_rows::{field, lines, name};
use common::membership_touch::{clock, location, model};

const FILE: &str = "membership_touch.jsonl";
const TOK: &str = "ABCDEFGHJKMNPQRSTVWXYZ0123";

fn base(live: &[(&str, &str)], writer: Option<TokenWriter>) -> Model {
    let all = lines(FILE);
    let header = all
        .iter()
        .find(|l| name(field(l, "seq")) == "header")
        .expect("a header");
    model(
        tables_of(field(header, "base")),
        None,
        live.iter().map(|(p, s)| (cps(p), cps(s))).collect(),
        writer,
        clock(field(header, "now")),
    )
}

/// The methods the store saw from `start`, a record by its event kind.
fn calls(m: &Model, start: usize) -> Vec<String> {
    m.store().0.borrow().log[start..]
        .iter()
        .map(|e| match e {
            PyValue::List(x) if name(&x[0]) == "record" => format!("record {}", name(&x[1])),
            PyValue::List(x) => name(&x[0]),
            other => panic!("{other:?}"),
        })
        .collect()
}

fn touch(m: &mut Model, id: &str, root: &str) -> (Checkout, Vec<String>) {
    let start = m.store().0.borrow().log.len();
    let k = m.checkout(&cps(id)).expect("a checkout");
    let spec = PyValue::Dict(vec![(PyValue::str("root"), PyValue::str(root))]);
    let k = m.touch_view(k, &location(&spec)).expect("a sighting");
    (k, calls(m, start))
}

fn one(id: &'static str, token: Option<&str>, status: &str) -> TokenWriter {
    let w = (token.map(cps), cps(status));
    Box::new(move |k: &Checkout| Ok((k.id == cps(id)).then(|| w.clone())))
}

#[test]
fn a_sighting_one_day_old_is_stale() {
    let mut m = base(&[], None);
    let (k, seen) = touch(&mut m, "K2", "/w/c");
    assert_eq!(seen, ["rows", "rows", "next_number", "put"]);
    assert_eq!(k.views[0].last_seen, cps("2026-03-04T04:06:07.123456Z"));
}

#[test]
fn a_sighting_a_microsecond_younger_is_fresh() {
    let mut m = base(&[], None);
    let (_, seen) = touch(&mut m, "K3", "/w/r");
    // The put is the retired flag cleared, not the view.
    assert_eq!(seen, ["rows", "rows", "put"]);
}

#[test]
fn a_sighting_that_does_not_parse_is_stale() {
    let mut m = base(&[], None);
    let (k, seen) = touch(&mut m, "K5", "/w/e2");
    assert_eq!(seen.last().map(String::as_str), Some("put"));
    assert!(k.views.iter().any(|v| v.order > 7));
}

#[test]
fn an_older_view_is_written_again_when_fresh() {
    let mut m = base(&[], None);
    let (k, seen) = touch(&mut m, "K1", "/w/a");
    assert_eq!(seen, ["rows", "rows", "next_number", "put"]);
    let a = k.views.iter().find(|v| v.path == cps("/w/a")).unwrap();
    assert!(a.order > 9);
    // The view keeps its place among the checkout's views.
    assert_eq!(k.views[0].path, cps("/w/a"));
}

#[test]
fn a_legacy_live_view_makes_a_new_path_an_added_view() {
    let mut m = base(&[("/w/g1", "live_legacy"), ("/w/g2", "gone")], None);
    let (_, seen) = touch(&mut m, "K4", "/w/new");
    assert!(seen.contains(&"record checkout_view_added".to_string()));
    let mut m = base(&[("/w/g1", "replaced"), ("/w/g2", "gone")], None);
    let (_, seen) = touch(&mut m, "K4", "/w/new");
    assert!(seen.contains(&"record checkout_moved".to_string()));
}

#[test]
fn the_previous_view_is_the_newest_here() {
    let mut m = base(&[("/w/g1", "gone"), ("/w/g2", "gone")], None);
    let start = m.store().0.borrow().log.len();
    touch(&mut m, "K4", "/w/new");
    let log = m.store().0.borrow().log[start..].to_vec();
    let rec = log
        .iter()
        .find(|e| matches!(e, PyValue::List(x) if name(&x[0]) == "record"))
        .unwrap();
    let PyValue::List(x) = rec else { panic!() };
    assert_eq!(x[2].get("previous"), Some(&PyValue::str("/w/g2")));
}

#[test]
fn a_new_path_with_no_view_here_is_added() {
    let mut m = base(&[("/w/a", "gone"), ("/w/b", "gone")], None);
    let start = m.store().0.borrow().log.len();
    let k = m.checkout(&cps("K1")).unwrap();
    let spec = PyValue::Dict(vec![
        (PyValue::str("root"), PyValue::str("/z")),
        (PyValue::str("machine"), PyValue::str("M9")),
    ]);
    m.touch_view(k, &location(&spec)).unwrap();
    assert!(calls(&m, start).contains(&"record checkout_view_added".to_string()));
}

#[test]
fn give_token_takes_a_superseded_status_where_token_written_does_not() {
    let mut m = base(&[], Some(one("K6", None, "superseded")));
    let k = m.checkout(&cps("K6")).unwrap();
    let start = m.store().0.borrow().log.len();
    m.give_token(&k).unwrap();
    assert_eq!(calls(&m, start), ["put", "record token_reminted"]);
    let mut m = base(&[], None);
    let start = m.store().0.borrow().log.len();
    m.token_written(&cps("K6"), None, &cps("superseded"))
        .unwrap();
    assert!(!calls(&m, start).contains(&"put".to_string()));
}

#[test]
fn a_write_left_to_another_machine_records_nothing() {
    let mut m = base(&[], Some(one("K7", Some(TOK), OTHER_MACHINE_SKIP)));
    let k = m.checkout(&cps("K7")).unwrap();
    let start = m.store().0.borrow().log.len();
    m.give_token(&k).unwrap();
    assert_eq!(calls(&m, start), ["put"]);
    assert_eq!(m.remint(), None);
}

#[test]
fn a_contended_write_is_recorded_unwritten() {
    let mut m = base(&[], Some(one("K7", Some(TOK), CONTENDED)));
    let k = m.checkout(&cps("K7")).unwrap();
    let start = m.store().0.borrow().log.len();
    m.give_token(&k).unwrap();
    assert_eq!(calls(&m, start), ["put", "record token_reminted"]);
    let log = m.store().0.borrow().log.clone();
    let PyValue::List(x) = log.last().unwrap() else {
        panic!()
    };
    assert_eq!(x[2].get("written"), Some(&PyValue::Bool(false)));
}

#[test]
fn no_write_leaves_the_remint_to_the_caller() {
    let mut m = base(&[], Some(one("K1", Some(TOK), "written")));
    let k = m.checkout(&cps("K2")).unwrap();
    m.give_token(&k).unwrap();
    assert_eq!(m.remint(), Some(&cps("K2")));
    let k = m.checkout(&cps("K1")).unwrap();
    m.give_token(&k).unwrap();
    // A written token does not clear it: only a call does.
    assert_eq!(m.remint(), Some(&cps("K2")));
}

#[test]
fn foreign_is_only_an_exists() {
    let m = base(&[], None);
    let k = m.checkout(&cps("K7")).unwrap();
    let other: PyStr = cps("0123456789ABCH797N8CD1RACY");
    for status in ["written", "Exists", "exists ", CONTENDED] {
        assert!(!foreign::<PyStr>(&k, Some(&other), &cps(status)).unwrap());
    }
    assert!(foreign::<PyStr>(&k, Some(&other), &cps("exists")).unwrap());
    assert!(foreign::<PyStr>(&k, None, &cps("exists")).unwrap());
}

#[test]
fn the_name_is_the_newest_views_base_name() {
    let m = base(&[], None);
    let k = m.checkout(&cps("K1")).unwrap();
    // The view on M2 has the highest order: its base name, not this machine's.
    assert_eq!(m.checkout_name(&k), cps("b"));
    let k = m.checkout(&cps("K7")).unwrap();
    assert_eq!(m.checkout_name(&k), cps("x"));
}

#[test]
fn a_raise_of_the_writer_passes_out_of_give_token() {
    let w: TokenWriter = Box::new(|_: &Checkout| {
        Err(Raised {
            class: "OSError",
            message: "no".into(),
        })
    });
    let mut m = base(&[], Some(w));
    let k = m.checkout(&cps("K6")).unwrap();
    let r = m.give_token(&k);
    assert!(matches!(r, Err(Error::Raised(e)) if e.class == "OSError"));
}
