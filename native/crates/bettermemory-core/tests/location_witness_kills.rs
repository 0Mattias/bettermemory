//! Cases a plausible wrong port of `location_witness`'s history half
//! fails: each states what the reference does where a natural reading of
//! it would answer otherwise.

use std::collections::BTreeSet;

use bettermemory_core::json::loads;
use bettermemory_core::location_witness::{
    answered_history, independent_answer, nonce_ok, AnsweredGit, AnsweredHistory, ExchangeReads,
    Exchanges, History, NeedWitnesses, EXCHANGES_CAP, EXCHANGE_SECONDS,
};
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::unicode::cps;

fn load(text: &str) -> PyValue {
    loads(&cps(text)).expect("JSON")
}

fn ids(items: &[&str]) -> Vec<PyStr> {
    items.iter().map(|t| cps(t)).collect()
}

fn h(i: u8) -> String {
    format!("{i:x}").repeat(40)
}

fn need(text: &str) -> NeedWitnesses {
    NeedWitnesses {
        question: load(text),
    }
}

#[test]
fn ancestry_bounded_only_false_says_complete() {
    for (v, bounded) in [
        ("false", false),
        ("0", true),
        ("null", true),
        ("\"\"", true),
    ] {
        let a = load(&format!(
            r#"{{"ancestry_of": [], "ancestry_bounded": {v}}}"#
        ));
        let r = AnsweredHistory::new(&a)
            .unwrap()
            .reachable_among(&[])
            .unwrap();
        assert_eq!(r.bounded, bounded, "{v}");
        assert_eq!(r.handled, 0);
    }
}

#[test]
fn a_string_of_ids_is_its_characters() {
    let a = load(r#"{"asked": "Ab", "held": "B"}"#);
    let r = AnsweredHistory::new(&a).unwrap();
    assert_eq!(
        r.holds(&ids(&["a", "b"])).unwrap(),
        BTreeSet::from([cps("b")])
    );
    let e = r.holds(&ids(&["ab"])).unwrap_err();
    assert_eq!(e.question, load(r#"{"held": ["ab"]}"#));
}

#[test]
fn a_number_of_ids_raises_and_zero_is_none() {
    assert_eq!(
        AnsweredHistory::new(&load(r#"{"asked": 3}"#)).unwrap_err(),
        PyError::TypeError
    );
    assert!(AnsweredHistory::new(&load(r#"{"asked": 0, "held": false}"#)).is_ok());
    let e = AnsweredHistory::new(&load(r#"{"ancestry": [["a", "b", "c"]]}"#)).unwrap_err();
    assert_eq!(e, PyError::ValueError);
}

#[test]
fn the_fingerprint_keeps_its_case_and_parents_are_lowered() {
    let a = load(r#"{"walk": {"AB": ["FP", ["P"]], "c": [1, []]}}"#);
    let r = AnsweredHistory::new(&a).unwrap();
    assert_eq!(r.fingerprint(&cps("ab")).unwrap(), Some(cps("FP")));
    assert_eq!(r.parents(&cps("Ab")).unwrap(), vec![cps("p")]);
    assert_eq!(r.fingerprint(&cps("c")).unwrap(), None);
    let e = r.parents(&cps("D")).unwrap_err();
    assert_eq!(e.question, load(r#"{"walk": true, "commits": ["d"]}"#));
}

#[test]
fn an_empty_answer_is_an_answer() {
    let r = AnsweredHistory::new(&load(r#"{"blobs": {"A": []}}"#)).unwrap();
    assert_eq!(r.blobs(&cps("a")).unwrap(), Vec::<PyStr>::new());
}

#[test]
fn the_ancestry_question_names_what_was_asked_too() {
    let a = load(&format!(r#"{{"ancestry_of": ["{}"]}}"#, h(1)));
    let r = AnsweredHistory::new(&a).unwrap();
    let e = r
        .reachable_among(&ids(&[&h(2).to_uppercase(), "zz"]))
        .unwrap_err();
    assert_eq!(
        e.question,
        load(&format!(r#"{{"ancestry": ["{}", "{}"]}}"#, h(1), h(2)))
    );
}

#[test]
fn one_tie_is_asked_an_exchange() {
    let g = AnsweredGit::new(&load("{}"), 256).unwrap();
    let mut asked = BTreeSet::new();
    assert!(g
        .independent(&ids(&[&h(1), &h(2)]), Some(&mut asked))
        .is_err());
    assert_eq!(
        g.independent(&ids(&[&h(3), &h(4)]), Some(&mut asked))
            .unwrap(),
        None
    );
    assert_eq!(asked.len(), 1);
    // Without an exchange every set is asked, and nothing is recorded.
    assert!(g.independent(&ids(&[&h(3), &h(4)]), None).is_err());
    assert!(g.independent(&ids(&[&h(3), &h(4)]), None).is_err());
    // A single commit, or none, answers itself.
    assert_eq!(
        g.independent(&ids(&[&h(5)]), None).unwrap(),
        Some(vec![h(5)])
    );
}

#[test]
fn the_bound_is_inclusive() {
    let g = AnsweredGit::new(&load("{}"), 2).unwrap();
    assert!(g.independent(&ids(&[&h(1), &h(2)]), None).is_err());
    assert_eq!(
        g.independent(&ids(&[&h(1), &h(2), &h(3)]), None).unwrap(),
        None
    );
}

#[test]
fn a_top_outside_or_empty_is_no_answer() {
    let a = load(&format!(
        r#"{{"independent": [{{"of": ["{a}", "{b}"], "top": ["{c}"]}}, {{"of": ["{a}", "{c}"], "top": []}}]}}"#,
        a = h(1),
        b = h(2),
        c = h(3)
    ));
    let g = AnsweredGit::new(&a, 256).unwrap();
    assert_eq!(g.independent(&ids(&[&h(1), &h(2)]), None).unwrap(), None);
    assert_eq!(g.independent(&ids(&[&h(3), &h(1)]), None).unwrap(), None);
}

#[test]
fn answered_history_wants_true_and_an_exchange_key() {
    let git = |t: &str| matches!(answered_history(&load(t), 256).unwrap(), History::Git(_));
    assert!(git(r#"{"independent_ok": true, "exchange": null}"#));
    assert!(!git(r#"{"independent_ok": 1, "exchange": null}"#));
    assert!(!git(r#"{"independent_ok": true}"#));
}

#[test]
fn joined_keeps_the_first_flag_and_first_order() {
    let j = NeedWitnesses::joined(&[
        need(r#"{"remint": false, "held": ["b", "a"]}"#),
        need(r#"{"remint": true, "held": ["c", "a"]}"#),
    ])
    .unwrap();
    assert_eq!(
        j.question,
        load(r#"{"remint": false, "held": ["b", "a", "c"]}"#)
    );
}

#[test]
fn joined_compares_dicts_as_python_does() {
    let j = NeedWitnesses::joined(&[
        need(r#"{"v": [{"a": 1, "b": 2}]}"#),
        need(r#"{"v": [{"b": 2.0, "a": true}, {"path": 1}, {"path": 1.0}, {"path": 1, "x": 0}]}"#),
    ])
    .unwrap();
    assert_eq!(
        j.question,
        load(r#"{"v": [{"a": 1, "b": 2}, {"path": 1}, {"path": 1.0}]}"#)
    );
}

#[test]
fn independent_answer_replaces_the_set_and_sorts_once_each() {
    let q = load(&format!(
        r#"{{"independent": ["{}", "{}"]}}"#,
        h(2).to_uppercase(),
        h(1)
    ));
    let a = load(&format!(
        r#"{{"independent": [{{"of": ["{a}", "{b}"], "top": null}}, "x"]}}"#,
        a = h(1),
        b = h(2)
    ));
    let mut seen = Vec::new();
    let mut git = |of: &[PyStr]| {
        seen.push(of.to_vec());
        Some(vec![h(2), h(1), h(2)])
    };
    let got = independent_answer(&q, &a, Some(&mut git)).unwrap();
    let want = load(&format!(
        r#"{{"independent_ok": true, "independent": [{{"of": ["{a}", "{b}"], "top": ["{a}", "{b}"]}}]}}"#,
        a = h(1),
        b = h(2)
    ));
    assert_eq!(got, want);
    assert_eq!(seen, vec![ids(&[&h(1), &h(2)])]);
}

fn new(x: &mut Exchanges, a: &str, s: &str, now: f64, fresh: u8) -> Result<ExchangeReads, PyError> {
    ExchangeReads::new(&load(a), Some(cps(s)), cps("m"), x, now, [fresh; 16], 256)
}

#[test]
fn a_kept_exchange_expires_at_exactly_its_time() {
    let mut x = Exchanges::new();
    let mut e = new(&mut x, r#"{"exchange": null}"#, "s", 10.0, 0x11).unwrap();
    e.set_read(cps("f"), None);
    let n = e.carried(&mut x, 10.0).unwrap();
    assert_eq!(n, "11".repeat(16));
    let a = format!(r#"{{"exchange": "{n}"}}"#);
    let late = new(&mut x, &a, "s", 10.0 + EXCHANGE_SECONDS, 0x22).unwrap();
    assert_eq!(late.nonce, "22".repeat(16));
    assert!(late.reads.is_empty());
}

#[test]
fn another_owner_neither_takes_nor_drops() {
    let mut x = Exchanges::new();
    let mut e = new(&mut x, r#"{"exchange": null}"#, "s", 1.0, 0x11).unwrap();
    e.set_read(cps("f"), Some(cps("g")));
    let n = e.carried(&mut x, 1.0).unwrap();
    let a = format!(r#"{{"exchange": "{n}"}}"#);
    let other = new(&mut x, &a, "t", 2.0, 0x22).unwrap();
    assert!(other.reads.is_empty());
    assert_eq!(x.entries().len(), 1);
    let mine = new(&mut x, &a, "s", 2.0, 0x33).unwrap();
    assert_eq!(mine.nonce, n);
    assert_eq!(mine.reads, vec![(cps("f"), Some(cps("g")))]);
    assert!(x.entries().is_empty());
}

#[test]
fn a_silent_client_keeps_nothing_and_an_unread_round_carries_nothing() {
    let mut x = Exchanges::new();
    let mut e = new(&mut x, r#"{"exchange": null}"#, "s", 1.0, 0x11).unwrap();
    e.set_read(cps("f"), None);
    e.carried(&mut x, 1.0).unwrap();
    let mut silent = new(&mut x, r#"{"remint_ok": true}"#, "s", 1.0, 0x22).unwrap();
    assert!(silent.silent && x.entries().is_empty());
    silent.set_read(cps("f"), None);
    assert_eq!(silent.carried(&mut x, 1.0), None);
    let empty = new(&mut x, "{}", "s", 1.0, 0x33).unwrap();
    assert!(!empty.silent);
    assert_eq!(empty.carried(&mut x, 1.0), None);
}

#[test]
fn an_error_after_the_take_up_loses_the_exchange() {
    let mut x = Exchanges::new();
    let mut e = new(&mut x, r#"{"exchange": null}"#, "s", 1.0, 0x11).unwrap();
    e.set_read(cps("f"), None);
    let n = e.carried(&mut x, 1.0).unwrap();
    let bad = format!(r#"{{"exchange": "{n}", "held": 4}}"#);
    assert_eq!(
        new(&mut x, &bad, "s", 1.0, 0x22).unwrap_err(),
        PyError::TypeError
    );
    assert!(x.entries().is_empty());
}

#[test]
fn the_cap_drops_the_oldest() {
    let mut x = Exchanges::new();
    for i in 0..=EXCHANGES_CAP {
        let mut f = [0u8; 16];
        f[15] = i as u8;
        f[14] = (i >> 8) as u8;
        let mut e = ExchangeReads::new(
            &load(r#"{"exchange": null}"#),
            None,
            cps("m"),
            &mut x,
            0.0,
            f,
            256,
        )
        .unwrap();
        e.set_read(cps("f"), None);
        e.carried(&mut x, 0.0);
    }
    assert_eq!(x.entries().len(), EXCHANGES_CAP);
    assert_eq!(x.entries()[0].0, format!("{}01", "0".repeat(30)));
}

#[test]
fn only_lowercase_nonces_of_32() {
    assert!(nonce_ok(&cps(&"ab".repeat(16))));
    assert!(!nonce_ok(&cps(&"AB".repeat(16))));
    assert!(!nonce_ok(&cps(&"a".repeat(31))));
    assert!(!nonce_ok(&cps(&format!("{}\n", "a".repeat(32)))));
}

#[test]
fn carrying_drops_the_exchanges_whose_time_is_up() {
    // The reference's `carried` calls `_drop(now)` before it keeps the
    // exchange, so one expired by then is gone from `_EXCHANGES`.
    let mut x = Exchanges::new();
    let mut a = new(&mut x, r#"{"exchange": null}"#, "s", 0.0, 0x11).unwrap();
    a.set_read(cps("f"), None);
    let n = a.carried(&mut x, 0.0).unwrap();
    let named = format!(r#"{{"exchange": "{n}"}}"#);
    let mut b = new(&mut x, &named, "t", 1.0, 0x22).unwrap();
    assert_eq!(x.entries().len(), 1);
    b.set_read(cps("g"), None);
    let m = b.carried(&mut x, EXCHANGE_SECONDS + 1.0).unwrap();
    let kept: Vec<&str> = x.entries().iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(kept, vec![m.as_str()]);
}

#[test]
fn only_a_walk_mapping_is_walked() {
    // `_walked = isinstance(raw_walk, Mapping)`.
    for (text, want) in [
        (r#"{"walk": {}}"#, true),
        (r#"{"walk": []}"#, false),
        (r#"{"walk": null}"#, false),
        (r#"{"walk": "ab"}"#, false),
        ("{}", false),
    ] {
        assert_eq!(
            AnsweredHistory::new(&load(text)).unwrap().walked(),
            want,
            "{text}"
        );
    }
}
