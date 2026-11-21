//! Every case of `membership_shared.jsonl` against `membership_shared`:
//! the stamp and tick rules, the re-mint read over carriers given, and
//! the question-row and log-row predicates, and the digest over the
//! seeded stamp-set pairs. `membership_shared_python.jsonl` holds the
//! log cases nested past the interpreter's recursion depth, with no
//! digest.

mod common;
#[path = "common/membership_shared.rs"]
mod shared;

use bettermemory_core::json;
use bettermemory_core::membership_shared as ms;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use common::parity::Ledger;
use sha2::{Digest, Sha256};
use shared::*;

fn answer(case: &PyValue, asked: &mut Option<Vec<PyStr>>) -> PyValue {
    let f = name(field(case, "f"));
    let k = || checkout(field(case, "k"));
    let loc = || location(field(case, "loc"));
    let carriers = || -> Vec<_> {
        case.get("carriers")
            .map(|c| list(c).iter().map(checkout).collect())
            .unwrap_or_default()
    };
    let found = || match case.get("found") {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    };
    let b = PyValue::Bool;
    let token = |r: Result<bool, _>| match r {
        Ok(v) => b(v),
        Err(e) => PyValue::str(&format!("{e:?}")),
    };
    match f.as_str() {
        "same_file" => b(ms::same_file(
            &stamp(field(case, "a")),
            &stamp(field(case, "b")),
        )),
        "same_files" => b(ms::same_files(
            &stamps(field(case, "a")),
            &stamps(field(case, "b")),
        )),
        "ticked" => b(ms::ticked(&stamps(field(case, "a")))),
        "in_ticks" => b(ms::in_ticks_at(&k(), &loc())),
        "carried" => b(ms::carried_in_ticks(
            &checkout(field(case, "c")),
            &k(),
            &loc(),
        )),
        "read_here" => b(ms::read_here(&k(), &loc())),
        "moved_under" => b(ms::moved_under(&k(), &loc())),
        "read_as_now" => b(ms::read_as_now(
            &k(),
            &loc(),
            &found().unwrap(),
            &carriers(),
        )),
        "made_by" => token(ms::made_by(&k(), &found().unwrap(), &carriers())),
        "continues" => token(ms::continues(&k(), &loc(), &found().unwrap(), &carriers())),
        "shared_remint" => {
            let f = found();
            token(ms::shared_remint(&k(), &loc(), f.as_ref(), |t| {
                *asked = Some(t.to_vec());
                carriers()
            }))
        }
        "view_made" => {
            let f = found();
            token(ms::view_made(&k(), f.as_ref(), |t| {
                *asked = Some(t.to_vec());
                carriers()
            }))
        }
        "asked_where" => match ms::asked_where(list(field(case, "rows"))) {
            Ok(v) => b(v),
            Err(e) => PyValue::Str(row_error(&e)),
        },
        "joined" => {
            let rows = log_rows(field(case, "log"));
            let PyValue::Str(c) = field(case, "checkout") else {
                panic!()
            };
            match ms::joined_silently_by_token(c, &rows) {
                Ok(v) => b(v),
                Err(e) => PyValue::str(e.name()),
            }
        }
        "asks" => match ms::asks(&proposal(field(case, "kind"), field(case, "evidence"))) {
            Ok((a, s)) => PyValue::List(vec![PyValue::Str(a), PyValue::Str(s)]),
            Err(e) => PyValue::Str(row_error(&e)),
        },
        other => panic!("unknown function {other}"),
    }
}

fn want(case: &PyValue) -> PyValue {
    if let Some(e) = case.get("e") {
        return e.clone();
    }
    field(case, "r").clone()
}

fn check_cases(ledger: &mut Ledger, lines: &[PyValue]) {
    for (i, case) in lines.iter().enumerate() {
        let mut asked = None;
        let got = answer(case, &mut asked);
        let w = want(case);
        let mut same = got == w;
        if let Some(a) = case.get("asked") {
            let mine = asked.map_or(PyValue::None, |v| {
                PyValue::List(v.into_iter().map(PyValue::Str).collect())
            });
            same = same && mine == *a;
        }
        assert!(
            !ledger.differs_fatally(i, same),
            "case {i}: native {got:?} reference {w:?} in {case:?}"
        );
    }
}

#[test]
fn membership_shared_every_case() {
    let name_ = "membership_shared.jsonl";
    let mut lines = lines(name_);
    let header = lines.remove(0);
    let sweep = lines.pop().expect("the digest line");
    let count = int(field(&header, "cases")) as usize;
    assert_eq!(count, lines.len());
    assert!(count > 0);
    let mut ledger = Ledger::new("golden_membership_shared", name_);
    check_cases(&mut ledger, &lines);
    // The digest.
    assert_eq!(name(field(&sweep, "f")), "sweep");
    let n = int(field(&sweep, "n")) as usize;
    let mut rng = Lcg(int(field(&sweep, "seed")) as u64);
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        let a = draw(&mut rng);
        let b = draw(&mut rng);
        out.push(sweep_line(&a, &b));
    }
    let digest = hex(&Sha256::digest(out.join("\n").as_bytes()));
    assert_eq!(n, 3000);
    assert!(!ledger.differs_fatally("sweep", PyValue::str(&digest) == *field(&sweep, "o")));
    ledger.settle(count + 1, &[]);
    let _ = json::loads(&unicode::cps("0"));
}

#[test]
fn membership_shared_python_every_case() {
    let name_ = "membership_shared_python.jsonl";
    let mut lines = lines(name_);
    let header = lines.remove(0);
    let count = int(field(&header, "cases")) as usize;
    assert_eq!(count, lines.len());
    assert!(count > 0);
    let mut ledger = Ledger::new("golden_membership_shared", name_);
    check_cases(&mut ledger, &lines);
    ledger.settle(count, &[]);
}
