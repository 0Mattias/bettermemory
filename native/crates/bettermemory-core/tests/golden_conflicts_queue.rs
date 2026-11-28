//! Every case of `conflicts_queue.jsonl` against `conflicts::queue` over
//! the recording stub: `load`, `pending`, `file_pair`, `upsert_scan`,
//! `scan_conflicts`, `resolve`, `split_judgeable`,
//! `conflicts_pending_count` and `_judged_content_changed`, each with the
//! stub's calls in order, the table after it and the warnings; and the
//! seeded sweep of scan sequences by count and SHA-256.

mod common;

use bettermemory_core::conflicts::{
    judged_content_changed, memories_by_id, split_judgeable, ConflictCandidate, CONFLICTS_FILENAME,
};
use bettermemory_core::pyvalue::PyValue;
use common::conflicts_queue::{
    cand, dec, enc, memories, of_bits, outcome, queue_call, row, tv, Stub,
};
use common::parity::Ledger;
use common::{hex, jsonl};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn split(case: &Value) -> Value {
    let rows: Vec<ConflictCandidate> = case["rows"]
        .as_array()
        .expect("rows")
        .iter()
        .map(cand)
        .collect();
    let active: Vec<Value> = case["active"].as_array().expect("active").clone();
    let raise = case.get("raise").cloned();
    let mut asked = Vec::new();
    let got = split_judgeable(rows, |id: &[u32]| {
        let id = tv(id);
        asked.push(id.clone());
        if raise.as_ref() == Some(&id) {
            return Err(());
        }
        Ok(active.contains(&id))
    });
    let mut out = case.clone();
    out["r"] = match got {
        Ok((judgeable, omitted)) => json!([
            judgeable.iter().map(|c| tv(&c.id)).collect::<Vec<_>>(),
            omitted
        ]),
        Err(()) => json!({ "err": "RuntimeError" }),
    };
    out["asked"] = Value::Array(asked);
    out
}

fn answer(case: &Value, header: &Value) -> Value {
    let u = header["u"].as_str().expect("u");
    match case["q"].as_str().expect("q") {
        "split" => return split(case),
        "judged" => {
            let ms = memories(&case["m"], u);
            let mut out = case.clone();
            out["r"] = match judged_content_changed(&cand(&case["c"]), &memories_by_id(&ms)) {
                Ok(b) => json!(b),
                Err(e) => outcome(&e.into()),
            };
            return out;
        }
        "sweep" => return sweep(case, header),
        _ => {}
    }
    let mut stub = Stub::of(&case["stub"]);
    let mut warns = Vec::new();
    let path = header["path"].as_str().expect("path");
    let r = match queue_call(&mut stub, case, header, path, &mut warns) {
        Ok(v) => v,
        Err(e) => outcome(&e),
    };
    let mut out = case.clone();
    out["r"] = r;
    out["calls"] = Value::Array(stub.calls.clone());
    let table = stub.table_value();
    if table != case["stub"]["rows"] {
        out["table"] = table;
    }
    if !warns.is_empty() {
        out["warn"] = json!(warns);
    }
    out
}

/// `gen_pyre_tree.Lcg`.
struct Lcg(u64);

impl Lcg {
    fn draw(&mut self) -> usize {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (self.0 >> 33) as usize
    }
    fn pick<'v>(&mut self, pool: &'v [Value]) -> &'v Value {
        &pool[self.draw() % pool.len()]
    }
}

fn pool<'c>(case: &'c Value, key: &str) -> &'c [Value] {
    case[key].as_array().expect("a pool")
}

fn s(v: &Value) -> PyValue {
    PyValue::str(v.as_str().expect("a string"))
}

/// `gen_conflicts._sweep_row`.
fn sweep_row(lcg: &mut Lcg, case: &Value) -> Vec<(PyValue, PyValue)> {
    let mut row = vec![
        ("id", s(lcg.pick(pool(case, "pairs")))),
        ("a_id", s(lcg.pick(pool(case, "ids")))),
        ("b_id", s(lcg.pick(pool(case, "ids")))),
        ("summary_a", s(lcg.pick(pool(case, "summaries")))),
        ("summary_b", s(lcg.pick(pool(case, "summaries")))),
        (
            "similarity",
            PyValue::Float(of_bits(lcg.pick(pool(case, "sims")))),
        ),
        ("method", PyValue::str("jaccard")),
        ("detector", s(lcg.pick(pool(case, "detectors")))),
        ("created", s(lcg.pick(pool(case, "created")))),
        ("status", s(lcg.pick(pool(case, "statuses")))),
    ];
    let hashes: Vec<Value> = std::iter::once(Value::Null)
        .chain(pool(case, "hashes").iter().cloned())
        .collect();
    for (key, values) in [
        ("verdict_ts", pool(case, "verdict_ts")),
        ("note", pool(case, "notes")),
        ("verdict_hash_a", &hashes[..]),
        ("verdict_hash_b", &hashes[..]),
    ] {
        let v = lcg.pick(values);
        if !v.is_null() {
            row.push((key, s(v)));
        }
    }
    row.into_iter().map(|(k, v)| (PyValue::str(k), v)).collect()
}

fn from_row(raw: &[(PyValue, PyValue)]) -> ConflictCandidate {
    ConflictCandidate::from_dict(raw).expect("a drawn row reads")
}

/// `gen_conflicts._sweep_memories`, as the fixture spells memories.
fn sweep_memories(lcg: &mut Lcg, case: &Value) -> Value {
    let mut out = Vec::new();
    for mid in pool(case, "ids") {
        if !lcg.draw().is_multiple_of(4) {
            let body = lcg.pick(pool(case, "bodies")).clone();
            let updated = lcg.pick(pool(case, "updated")).clone();
            out.push(json!({ "i": mid, "b": body, "u": updated }));
        }
    }
    Value::Array(out)
}

/// `gen_conflicts._sweep_op`: one drawn call on `stub`, as its line.
fn sweep_op(lcg: &mut Lcg, stub: &mut Stub, case: &Value, header: &Value) -> Value {
    let kind = lcg.draw() % 4;
    stub.fail = if lcg.draw().is_multiple_of(8) {
        let op = ["put", "delete", "list", "count"][lcg.draw() % 4];
        let at = stub.n.get(op).copied().unwrap_or(0) + 1;
        Some((op.to_owned(), at, "RuntimeError".to_owned()))
    } else {
        None
    };
    stub.calls.clear();
    let c = match kind {
        0 => json!({ "q": "file", "cand": row(&from_row(&sweep_row(lcg, case))) }),
        1 => {
            let id = lcg.pick(pool(case, "pairs")).clone();
            let status = lcg.pick(pool(case, "verdicts")).clone();
            let mut bodies = Vec::new();
            for mid in pool(case, "ids") {
                if !lcg.draw().is_multiple_of(2) {
                    bodies.push(json!([mid, lcg.pick(pool(case, "bodies"))]));
                }
            }
            let note = lcg.pick(pool(case, "notes")).clone();
            let stamp = if !lcg.draw().is_multiple_of(2) {
                json!(["log"])
            } else {
                json!([])
            };
            json!({ "q": "resolve", "id": id, "status": status, "note": note,
                    "bodies": bodies, "stamp": stamp })
        }
        _ => {
            let mut fresh = Vec::new();
            for _ in 0..lcg.draw() % 4 {
                let mut raw = sweep_row(lcg, case);
                raw[9].1 = PyValue::str("pending");
                fresh.push(row(&from_row(&raw)));
            }
            let ms = sweep_memories(lcg, case);
            let n = ms.as_array().expect("memories").len() as i64;
            stub.counts = vec![n + (lcg.draw() % 3) as i64 - 1];
            json!({ "q": "upsert", "fresh": fresh, "m": ms })
        }
    };
    let mut warns = Vec::new();
    let path = header["path"].as_str().expect("path");
    let r = match queue_call(stub, &c, header, path, &mut warns) {
        Ok(v) => v,
        Err(e) => outcome(&e),
    };
    json!([stub.calls, r, warns, stub.table_value()])
}

/// `json.dumps(..., separators=(",", ":"), ensure_ascii=True)` of a line
/// that holds ASCII text and single-key or sorted objects.
fn dumps_ascii(v: &Value) -> String {
    let text = serde_json::to_string(v).expect("json");
    assert!(text.is_ascii(), "the sweep draws ASCII text");
    text
}

fn sweep(case: &Value, header: &Value) -> Value {
    let mut lcg = Lcg(case["seed"].as_u64().expect("seed"));
    let count = case["count"].as_u64().expect("count");
    let ops = case["ops"].as_u64().expect("ops");
    let mut digest = Sha256::new();
    let mut lines = 0;
    for _ in 0..count {
        let mut stub = Stub {
            counts: vec![0],
            ..Stub::default()
        };
        for _ in 0..lcg.draw() % 5 {
            stub.table.push(sweep_row(&mut lcg, case));
        }
        for _ in 0..ops {
            let line = sweep_op(&mut lcg, &mut stub, case, header);
            digest.update(dumps_ascii(&line).as_bytes());
            digest.update(b"\n");
            lines += 1;
        }
    }
    let mut out = case.clone();
    out["lines"] = json!(lines);
    out["sha256"] = json!(hex(&digest.finalize()));
    out
}

#[test]
fn conflicts_queue_every_case() {
    let name = "conflicts_queue.jsonl";
    let lines = jsonl(name);
    let header = &lines[0];
    assert_eq!(header["filename"], json!(CONFLICTS_FILENAME));
    // The record a `before_stamp` puts reads back as it is spelled.
    assert_eq!(enc(&dec(&header["stamp_record"])), header["stamp_record"]);
    let in_file = header["cases"].as_u64().expect("cases") as usize;
    assert_eq!(in_file, lines.len() - 1, "{name}: header count");
    let mut ledger = Ledger::new("conflicts_queue_every_case", name);
    for (i, case) in lines[1..].iter().enumerate() {
        ledger.case(format!("case {i} ({})", case["q"]), || {
            assert_eq!(answer(case, header), *case, "{name} case {i}");
        });
    }
    ledger.finish(in_file);
}
