//! Hostile input to `location_answer`: many and huge ids, deep values
//! carried through, integers past any machine word, and a growth claim
//! on the merge of earlier answers.

mod timing;
#[allow(dead_code)]
mod witness;

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::Instant;

use bettermemory_core::capture::{Capture, State};
use bettermemory_core::location_answer as la;
use bettermemory_core::location_git::Reach;
use bettermemory_core::location_reads::{Board, Clock};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use witness::cps;

struct Echo;

impl la::AnswerHistory for Echo {
    fn holds_objects(&mut self, ids: &[PyStr]) -> BTreeSet<PyStr> {
        ids.iter().cloned().collect()
    }
    fn fingerprint(&mut self, c: &[u32]) -> Option<PyStr> {
        Some(c.to_vec())
    }
    fn parents(&mut self, _: &[u32]) -> Vec<PyStr> {
        Vec::new()
    }
    fn reachable_among(&mut self, c: &[PyStr]) -> Reach {
        let all: Vec<String> = c
            .iter()
            .map(|x| {
                String::from_utf8_lossy(&x.iter().map(|&b| b as u8).collect::<Vec<_>>())
                    .into_owned()
            })
            .collect();
        Reach {
            relations: all
                .iter()
                .map(|a| (a.clone(), all.clone()))
                .take(5)
                .collect(),
            bounded: true,
            handled: 0,
        }
    }
    fn blobs(&mut self, c: &[u32]) -> Vec<PyStr> {
        vec![c.to_vec()]
    }
    fn independent(&mut self, of: &[PyStr]) -> Option<Vec<String>> {
        Some(of.iter().map(|_| "x".to_owned()).collect())
    }
}

fn board() -> Board<String, PyStr, String> {
    let start = Instant::now();
    let clock: Clock = Arc::new(move || start.elapsed().as_secs_f64());
    Board::new(clock)
}

fn repo() -> Capture {
    let mut cap = Capture::new(cps("/r"), State::Repo);
    cap.common_dir = Some(cps("/r/.git"));
    cap.head = Some(cps("c1"));
    cap
}

fn answer(
    q: &PyValue,
    a: &PyValue,
    b: &Board<String, PyStr, String>,
) -> Result<PyValue, la::AnswerError<String>> {
    let read: la::ViewRead<String> = Arc::new(|_: &[u32], _: &_| Ok(cps("live")));
    la::answer(
        q,
        a,
        &repo(),
        &mut |_: &[u32]| Echo,
        &mut |_: &[u32], _, _, _| (None, "skipped".into()),
        b,
        &read,
    )
}

fn dict(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

fn ids(n: usize) -> PyValue {
    PyValue::List((0..n).map(|i| PyValue::str(&format!("{i:040x}"))).collect())
}

#[test]
fn many_ids_in_every_field_are_answered() {
    let b = board();
    let q = dict(vec![
        ("held", ids(20_000)),
        ("commits", ids(20_000)),
        ("blobs", ids(20_000)),
        ("ancestry", ids(20_000)),
        ("independent", ids(2_000)),
    ]);
    let a = dict(vec![
        ("asked", ids(20_000)),
        ("walk", PyValue::Dict(Vec::new())),
    ]);
    let got = answer(&q, &a, &b).unwrap();
    let PyValue::Dict(out) = &got else { panic!() };
    assert!(out
        .iter()
        .any(|(k, v)| *k == PyValue::str("held")
            && matches!(v, PyValue::List(l) if l.len() == 20_000)));
}

#[test]
fn deep_and_huge_values_are_carried_or_refused_without_a_panic() {
    let b = board();
    let mut deep = PyValue::None;
    for _ in 0..5_000 {
        deep = PyValue::List(vec![deep]);
    }
    let a = dict(vec![("carried", deep)]);
    assert!(answer(&dict(vec![]), &a, &b).is_ok());
    let huge = PyValue::Int(format!("1{}", "0".repeat(5_000)));
    let a = dict(vec![(
        "asked",
        PyValue::List(vec![huge.clone(), PyValue::Int("-7".into()), huge]),
    )]);
    assert!(answer(&dict(vec![]), &a, &b).is_ok());
    let long = PyValue::Str(vec![0x41; 1 << 20]);
    let q = dict(vec![
        ("held", PyValue::List(vec![long.clone()])),
        ("commits", PyValue::List(vec![long])),
    ]);
    assert!(answer(&q, &dict(vec![]), &b).is_ok());
    let a = dict(vec![(
        "asked",
        PyValue::List(vec![PyValue::Float(f64::NAN), PyValue::Int("1".into())]),
    )]);
    assert!(answer(&dict(vec![]), &a, &b).is_ok());
}

#[test]
fn merging_earlier_answers_grows_about_linearly() {
    let b = board();
    let run = |n: usize| {
        let walk = PyValue::Dict(
            (0..n)
                .map(|i| (PyValue::str(&format!("{i:040x}")), PyValue::None))
                .collect(),
        );
        let a = dict(vec![
            ("asked", ids(n)),
            ("walk", walk),
            ("blobs", PyValue::Dict(Vec::new())),
        ]);
        let q = dict(vec![
            ("commits", ids(n)),
            ("walk", PyValue::Bool(true)),
            ("held", ids(n)),
        ]);
        (q, a)
    };
    let (qs, as_) = run(2_000);
    let (ql, al) = run(20_000);
    timing::grows_within(
        "answer: 2,000 and 20,000 earlier ids",
        timing::TENFOLD,
        &mut || timing::once(|| answer(&qs, &as_, &b).unwrap()),
        &mut || timing::once(|| answer(&ql, &al, &b).unwrap()),
    );
}
