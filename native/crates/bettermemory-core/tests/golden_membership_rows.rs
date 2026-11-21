//! Every case of `membership_rows.jsonl` (not gated) and
//! `membership_rows_python.jsonl` (gated on the Python release) against
//! the native `membership_rows`, and the digest over 2,000 checkout rows.
//! The spelling is the generator's (`tests/contract/gen_membership_rows.py`).

mod common;

use bettermemory_core::checkout_token::TOKEN_NAME;
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::membership_rows as rows;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode;
use common::membership_rows::*;
use common::parity::Ledger;
use sha2::{Digest, Sha256};

fn run(test: &str, file: &str, records: &[PyValue], header: &PyValue) -> usize {
    let mut ledger = Ledger::new(test, file);
    let bases = field(header, "bases");
    let full = field(header, "full");
    let mut total = 0;
    for (i, case) in records.iter().enumerate() {
        let f = name(field(case, "f"));
        if f == "digest" {
            continue;
        }
        let want = field(case, "o");
        let got = match f.as_str() {
            "jsonable" => match rows::jsonable(&dec(field(case, "a"))) {
                Ok(v) => enc(&v),
                Err(e) => raised(&e),
            },
            "location" => {
                let loc = location(field(case, "a"));
                let unwritten = rows::unwritten(&loc).map_or(PyValue::None, PyValue::Str);
                PyValue::List(vec![enc(&rows::signature(&loc)), enc(&unwritten)])
            }
            _ => {
                let row = case_row(bases, case);
                let views: Vec<_> = case
                    .get("views")
                    .map(|v| list(v).iter().map(view_of).collect())
                    .unwrap_or_default();
                answer(&f, &row, &views)
            }
        };
        let want = match f.as_str() {
            "jsonable" | "location" => want.clone(),
            _ => expanded(&f, full, want),
        };
        total += 1;
        let label = format!("line {} {f} {:?}", i + 2, case.get("d").or(case.get("a")));
        assert!(
            !ledger.differs_fatally(&label, same(&got, &want)),
            "{label}: got {got:?}, want {want:?}"
        );
    }
    ledger.finish(total);
    total
}

#[test]
fn membership_rows_every_case() {
    let all = lines("membership_rows.jsonl");
    let header = &all[0];
    assert_eq!(name(field(header, "ticks_key")), rows::TICKS_KEY);
    assert_eq!(name(field(header, "token_name")), TOKEN_NAME);
    let cases = &all[1..all.len() - 1];
    assert_eq!(int(field(header, "count")) as usize, cases.len());
    let done = run(
        "membership_rows_every_case",
        "membership_rows.jsonl",
        cases,
        header,
    );
    assert_eq!(done, cases.len());
}

#[test]
fn membership_rows_digest() {
    let all = lines("membership_rows.jsonl");
    let d = all.last().expect("the digest");
    assert_eq!(name(field(d, "f")), "digest");
    let pools: Vec<(PyValue, Vec<PyValue>)> = list(field(d, "pools"))
        .iter()
        .map(|p| {
            (
                list(p)[0].clone(),
                list(&list(p)[1]).iter().map(dec).collect(),
            )
        })
        .collect();
    let count = int(field(d, "count")) as usize;
    let mut rng = Lcg(int(field(d, "seed")) as u64);
    let options = DumpOptions {
        sort_keys: true,
        item_separator: ",",
        key_separator: ":",
        ..DumpOptions::DEFAULT
    };
    let mut h = Sha256::new();
    let mut n = 0;
    for _ in 0..count {
        let mut row: Vec<(PyValue, PyValue)> = pools
            .iter()
            .map(|(k, pool)| (k.clone(), pool[rng.next(pool.len())].clone()))
            .collect();
        if rng.next(20) == 0 {
            let gone = pools[rng.next(pools.len())].0.clone();
            row.retain(|(k, _)| *k != gone);
        }
        let o = kept(answer("checkout", &PyValue::Dict(row), &[]));
        let text = json::dumps(&o, options).expect("dumps");
        h.update(unicode::to_string(&text).expect("ASCII").as_bytes());
        h.update(b"\n");
        n += 1;
    }
    assert!(n > 0);
    assert_eq!(n, count);
    assert_eq!(hex(&h.finalize()), name(field(d, "sha256")));
}

#[test]
fn membership_rows_python_every_case() {
    let all = lines("membership_rows_python.jsonl");
    let base = lines("membership_rows.jsonl");
    let cases = &all[1..];
    assert_eq!(int(field(&all[0], "count")) as usize, cases.len());
    let done = run(
        "membership_rows_python_every_case",
        "membership_rows_python.jsonl",
        cases,
        &base[0],
    );
    assert_eq!(done, cases.len());
}
