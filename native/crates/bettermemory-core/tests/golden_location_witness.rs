//! Every case of `location_witness.jsonl` against `location_witness`: the
//! readers over crafted answers and their calls, `NeedWitnesses::joined`, `independent_answer` and sequences of
//! exchange rounds over one `Exchanges`, each answer compared as the JSON
//! text the reference's `json.dumps` writes.

mod common;

use bettermemory_core::json::{dumps, loads, DumpOptions};
use bettermemory_core::location_git::Reach;
use bettermemory_core::location_witness::{
    answered_history, independent_answer, AnsweredGit, AnsweredHistory, ExchangeReads, Exchanges,
    History, Independent, NeedWitnesses,
};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;

fn load(text: &str) -> PyValue {
    loads(&unicode::cps(text)).expect("a JSON text")
}

fn dump(v: &PyValue) -> String {
    unicode::to_string(&dumps(v, DumpOptions::DEFAULT).expect("dumps")).expect("ASCII")
}

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn d(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(pairs.into_iter().map(|(k, v)| (s(k), v)).collect())
}

fn strs<'a>(items: impl IntoIterator<Item = &'a String>) -> PyValue {
    PyValue::List(items.into_iter().map(|t| s(t)).collect())
}

fn cps_list<'a>(items: impl IntoIterator<Item = &'a PyStr>) -> PyValue {
    PyValue::List(items.into_iter().map(|t| PyValue::Str(t.clone())).collect())
}

fn texts(v: &PyValue) -> Vec<PyStr> {
    match v {
        PyValue::List(items) => items
            .iter()
            .map(|i| match i {
                PyValue::Str(t) => t.clone(),
                other => panic!("not a string: {other:?}"),
            })
            .collect(),
        other => panic!("not a list: {other:?}"),
    }
}

fn text(v: &PyValue) -> PyStr {
    match v {
        PyValue::Str(t) => t.clone(),
        other => panic!("not a string: {other:?}"),
    }
}

fn answered<T>(r: Result<T, NeedWitnesses>, enc: impl FnOnce(T) -> PyValue) -> PyValue {
    match r {
        Ok(v) => d(vec![("ok", enc(v))]),
        Err(n) => d(vec![("need", n.question)]),
    }
}

fn reach(r: Reach) -> PyValue {
    let rel = r
        .relations
        .iter()
        .map(|(c, from)| PyValue::List(vec![s(c), strs(from)]))
        .collect();
    d(vec![
        ("rel", PyValue::List(rel)),
        ("b", PyValue::Bool(r.bounded)),
    ])
}

fn independent(r: Independent) -> PyValue {
    answered(r, |t| match t {
        None => PyValue::None,
        Some(t) => strs(&t),
    })
}

fn call(h: &AnsweredHistory, git: Option<&AnsweredGit>, name: &str, arg: &PyValue) -> PyValue {
    match name {
        "holds" => answered(h.holds(&texts(arg)), |v| cps_list(&v)),
        "holds_objects" => answered(h.holds_objects(&texts(arg)), |v| cps_list(&v)),
        "fingerprint" => answered(h.fingerprint(&text(arg)), |v| match v {
            None => PyValue::None,
            Some(t) => PyValue::Str(t),
        }),
        "parents" => answered(h.parents(&text(arg)), |v| cps_list(&v)),
        "blobs" => answered(h.blobs(&text(arg)), |v| cps_list(&v)),
        "reach" => answered(h.reachable_among(&texts(arg)), reach),
        "independent" => independent(git.expect("a git reader").independent(&texts(arg), None)),
        other => panic!("unknown call {other}"),
    }
}

fn history_row(row: &Value) -> String {
    let a = load(row["a"].as_str().unwrap());
    let ops = load(row["o"].as_str().unwrap());
    let bound = row.get("b").and_then(Value::as_u64).unwrap_or(256) as usize;
    let built = match row["c"].as_str().unwrap() {
        "h" => AnsweredHistory::new(&a).map(History::Plain),
        "g" => AnsweredGit::new(&a, bound).map(History::Git),
        _ => answered_history(&a, bound),
    };
    let hist = match built {
        Err(e) => return dump(&d(vec![("err", s(e.name()))])),
        Ok(h) => h,
    };
    let (cls, git) = match &hist {
        History::Plain(_) => ("AnsweredHistory", None),
        History::Git(g) => ("AnsweredGit", Some(g)),
    };
    let PyValue::List(ops) = &ops else {
        panic!("ops")
    };
    let out = ops
        .iter()
        .map(|op| {
            let PyValue::List(pair) = op else {
                panic!("op")
            };
            let name = unicode::to_string(&text(&pair[0])).unwrap();
            call(hist.reader(), git, &name, &pair[1])
        })
        .collect();
    dump(&d(vec![("cls", s(cls)), ("ops", PyValue::List(out))]))
}

fn joined_row(row: &Value) -> String {
    let n = load(row["n"].as_str().unwrap());
    let PyValue::List(qs) = &n else { panic!("n") };
    let needs: Vec<NeedWitnesses> = qs
        .iter()
        .map(|q| NeedWitnesses {
            question: q.clone(),
        })
        .collect();
    match NeedWitnesses::joined(&needs) {
        Ok(n) => dump(&d(vec![("ok", n.question)])),
        Err(e) => dump(&d(vec![("err", s(e.name()))])),
    }
}

fn ia_row(row: &Value) -> String {
    let q = load(row["q"].as_str().unwrap());
    let a = load(row["a"].as_str().unwrap());
    let mut calls: Vec<Vec<PyStr>> = Vec::new();
    let h = row.get("h");
    let mut stub = |of: &[PyStr]| -> Option<Vec<String>> {
        calls.push(of.to_vec());
        match h.unwrap() {
            Value::String(_) => None,
            Value::Array(items) => Some(
                items
                    .iter()
                    .map(|i| i.as_str().unwrap().to_owned())
                    .collect(),
            ),
            other => panic!("h {other}"),
        }
    };
    let got = independent_answer(&q, &a, if h.is_some() { Some(&mut stub) } else { None });
    let calls = PyValue::List(calls.iter().map(cps_list).collect());
    match got {
        Ok(v) => dump(&d(vec![("ok", v), ("calls", calls)])),
        Err(e) => dump(&d(vec![("err", s(e.name())), ("calls", calls)])),
    }
}

fn fresh_of(hex: &str) -> [u8; 16] {
    let b = common::unhex(hex);
    b.try_into().expect("16 bytes")
}

fn opt(v: &Option<PyStr>) -> PyValue {
    match v {
        None => PyValue::None,
        Some(t) => PyValue::Str(t.clone()),
    }
}

fn asked_of(a: &std::collections::BTreeSet<Vec<String>>) -> PyValue {
    PyValue::List(a.iter().map(strs).collect())
}

fn reads_of(r: &[(PyStr, Option<PyStr>)]) -> PyValue {
    PyValue::List(
        r.iter()
            .map(|(f, v)| PyValue::List(vec![PyValue::Str(f.clone()), opt(v)]))
            .collect(),
    )
}

fn kept_state(x: &Exchanges) -> PyValue {
    let e = x.entries();
    if e.len() <= 4 {
        return PyValue::List(
            e.iter()
                .map(|(n, k)| {
                    PyValue::List(vec![
                        s(n),
                        PyValue::List(vec![opt(&k.owner.0), PyValue::Str(k.owner.1.clone())]),
                        PyValue::Float(k.until),
                        reads_of(&k.reads),
                        asked_of(&k.asked),
                    ])
                })
                .collect(),
        );
    }
    d(vec![
        ("n", PyValue::int(e.len() as i128)),
        (
            "first",
            PyValue::List(e[..2].iter().map(|(n, _)| s(n)).collect()),
        ),
        ("last", s(&e[e.len() - 1].0)),
    ])
}

fn exchange_row(row: &Value) -> String {
    let mut x = Exchanges::new();
    let mut now = 0.0;
    let mut fresh = [0u8; 16];
    let mut cur: Option<ExchangeReads> = None;
    let mut out = Vec::new();
    for st in row["s"].as_array().unwrap() {
        if let Some(n) = st.get("now") {
            now = n.as_f64().unwrap();
        }
        if let Some(f) = st.get("fresh") {
            fresh = fresh_of(f.as_str().unwrap());
        }
        let ans = match st["do"].as_str().unwrap() {
            "new" => {
                let a = load(st["a"].as_str().unwrap());
                let session = st["s"].as_str().map(unicode::cps);
                let machine = unicode::cps(st["m"].as_str().unwrap());
                match ExchangeReads::new(&a, session, machine, &mut x, now, fresh, 256) {
                    Ok(e) => {
                        let hist = match e.history {
                            History::Plain(_) => "AnsweredHistory",
                            History::Git(_) => "AnsweredGit",
                        };
                        let v = d(vec![
                            ("nonce", s(&e.nonce)),
                            ("silent", PyValue::Bool(e.silent)),
                            ("reads", reads_of(&e.reads)),
                            ("asked", asked_of(&e.asked)),
                            ("hist", s(hist)),
                        ]);
                        cur = Some(e);
                        v
                    }
                    Err(err) => {
                        cur = None;
                        d(vec![("err", s(err.name()))])
                    }
                }
            }
            "read" => {
                let f = unicode::cps(st["f"].as_str().unwrap());
                let v = st["v"].as_str().map(unicode::cps);
                cur.as_mut().unwrap().set_read(f, v);
                PyValue::None
            }
            "ind" => {
                let c: Vec<PyStr> = st["c"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|c| unicode::cps(c.as_str().unwrap()))
                    .collect();
                match cur.as_mut().unwrap().independent(&c) {
                    None => s("plain"),
                    Some(r) => independent(r),
                }
            }
            "carry" => match cur.as_ref().unwrap().carried(&mut x, now) {
                None => PyValue::None,
                Some(n) => s(&n),
            },
            _ => {
                let a = d(vec![("exchange", PyValue::None)]);
                for i in 0..st["n"].as_u64().unwrap() {
                    let f = (i as u128).to_be_bytes();
                    let mut e = ExchangeReads::new(
                        &a,
                        Some(unicode::cps("s")),
                        unicode::cps("m"),
                        &mut x,
                        now,
                        f,
                        256,
                    )
                    .unwrap();
                    e.set_read(unicode::cps("f"), None);
                    e.carried(&mut x, now);
                }
                PyValue::None
            }
        };
        out.push(d(vec![("a", ans), ("kept", kept_state(&x))]));
    }
    dump(&PyValue::List(out))
}

fn run(test: &str, file: &str) {
    let lines = jsonl(file);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    let mut total = 0;
    for (i, row) in cases.iter().enumerate() {
        let got = match row["k"].as_str().unwrap() {
            "h" => history_row(row),
            "j" => joined_row(row),
            "ia" => ia_row(row),
            "x" => exchange_row(row),
            other => panic!("unknown kind {other}"),
        };
        total += 1;
        let want = row["r"].as_str().unwrap();
        if got != want {
            failures.push(format!(
                "#{i} {}: native {got} reference {want}",
                row["note"]
            ));
        }
    }
    common::assert_count(file, total, cases.len());
    ledger.settle(total, &failures);
}

#[test]
fn location_witness_every_case() {
    run("location_witness_every_case", "location_witness.jsonl");
}
