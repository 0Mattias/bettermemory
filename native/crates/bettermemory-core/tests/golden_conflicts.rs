//! Every case of `conflicts.jsonl` and `conflicts_unicode.jsonl` against
//! `conflicts`: the hashes, the negation and clause readings, the body
//! signals and the guards over a pair, `pick_keeper`, `from_dict` and
//! `to_dict`, the scan and `find_conflict_candidates`, and the seeded
//! sweep of memory sets by count and digest.

mod common;

use bettermemory_core::conflicts::{
    body_hash, body_signals, clause_polarity, conflict_signal, find_conflict_candidates,
    find_dedup_with_skips, has_negation, numeric_divergence, pair_id, pick_keeper, polarity_flip,
    ConflictCandidate, ConflictError, DedupCandidate, PolaritySkippedPair, CLAUSE_SPLIT_PATTERN,
    DEFAULT_JACCARD_THRESHOLD, NEGATION_MARKERS, NEGATION_TOKEN_PATTERN, VALID_STATUSES,
};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::{repr, PyStr, PyValue};
use bettermemory_core::records::{Confidence, Memory, RecordFields, Source};
use bettermemory_core::unicode;
use common::parity::Ledger;
use common::{jsonl, neutral, text_field};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn bits(x: f64) -> String {
    format!("{:016x}", x.to_bits())
}

fn float_of_bits(v: &Value) -> f64 {
    f64::from_bits(u64::from_str_radix(v.as_str().expect("bits"), 16).expect("hex"))
}

/// An answer's text: a string, or its code points where it holds a
/// surrogate.
fn tv(t: &[u32]) -> Value {
    match unicode::to_string(t) {
        Some(s) => json!(s),
        None => json!({ "cps": t }),
    }
}

fn dt(text: &str) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(text)).expect("an isoformat")
}

fn memory(id: PyStr, body: PyStr, updated: &str, paths: &[&str], last: Option<&str>) -> Memory {
    Memory {
        fields: RecordFields {
            id,
            created: dt("2024-03-01T12:00:00+00:00"),
            updated: dt(updated),
            scopes: vec![unicode::cps("global")],
            confidence: Confidence::MEMBERS[0].1,
            source: Source::MEMBERS[0].1,
            body,
            origin: None,
            actor: None,
            last_verified_at: last.map(dt),
            category: None,
            verified_paths: paths.iter().map(|p| unicode::cps(p)).collect(),
            verified_commits: Vec::new(),
            verified_versions: Vec::new(),
            verified_absent_paths: Vec::new(),
            verified_path_targets: None,
            verified_absent_path_targets: None,
            claims: Vec::new(),
            verified_head: None,
        },
        links: Vec::new(),
        corroborations: "0".to_owned(),
        last_corroborated: None,
    }
}

/// A case's memory `{"i", "b", "u", "v", "l"}`.
fn memory_of(m: &Value, u: &str) -> Memory {
    let paths: Vec<&str> = m["v"]
        .as_array()
        .map(|a| a.iter().map(|p| p.as_str().expect("a path")).collect())
        .unwrap_or_default();
    memory(
        text_field(m, "i"),
        text_field(m, "b"),
        m["u"].as_str().unwrap_or(u),
        &paths,
        m["l"].as_str(),
    )
}

fn err(e: &ConflictError) -> Value {
    json!({ "err": e.name() })
}

fn sorted(set: impl IntoIterator<Item = String>) -> Value {
    let mut v: Vec<String> = set.into_iter().collect();
    v.sort();
    json!(v)
}

fn repr_text(value: &PyValue) -> Value {
    json!(unicode::to_string(&repr(value).expect("a repr")).expect("repr escapes surrogates"))
}

/// `json.dumps(..., separators=(",", ":"), ensure_ascii=True)`.
fn dumps_ascii(v: &Value) -> String {
    let text = serde_json::to_string(v).expect("json");
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        if c.is_ascii() {
            out.push(c);
        } else {
            let mut units = [0u16; 2];
            for u in c.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{u:04x}"));
            }
        }
    }
    out
}

/// The `[d, f]` of a `scan` case.
fn scan(ms: &[Memory], th: Option<f64>, now: &DateTime) -> (Value, Value) {
    let d = match find_dedup_with_skips(ms, th) {
        Ok((cands, skips, method)) => {
            let cands: Vec<Value> = cands
                .iter()
                .map(|c| {
                    json!([
                        tv(&c.keeper_id),
                        tv(&c.duplicate_id),
                        bits(c.similarity),
                        tv(&c.keeper_summary),
                        tv(&c.duplicate_summary)
                    ])
                })
                .collect();
            let skips: Vec<Value> = skips
                .iter()
                .map(|p| {
                    json!([
                        tv(&p.memory_id_a),
                        tv(&p.memory_id_b),
                        bits(p.similarity),
                        p.detector,
                        tv(&p.summary_a),
                        tv(&p.summary_b)
                    ])
                })
                .collect();
            json!([cands, skips, method])
        }
        Err(e) => err(&e),
    };
    let f = match find_conflict_candidates(ms, th, now) {
        Ok(rows) => Value::Array(rows.iter().map(|r| repr_text(&r.to_dict())).collect()),
        Err(e) => err(&e),
    };
    (d, f)
}

fn opt_text(v: &Value) -> Option<PyStr> {
    v.as_str().map(unicode::cps)
}

fn s(v: &Value) -> PyStr {
    unicode::cps(v.as_str().expect("a string"))
}

fn leak(v: &Value) -> &'static str {
    Box::leak(v.as_str().expect("a string").to_string().into_boxed_str())
}

fn float_arg(v: &Value) -> f64 {
    match neutral(v) {
        PyValue::Float(f) => f,
        other => panic!("not a float: {other:?}"),
    }
}

/// `repr(x.to_dict())` of a `dict` case's object.
fn to_dict(c: &Value) -> Value {
    let a = c.as_array().expect("args");
    let dict = match a[0].as_str().expect("kind") {
        "cand" => ConflictCandidate {
            id: s(&a[1]),
            a_id: s(&a[2]),
            b_id: s(&a[3]),
            summary_a: s(&a[4]),
            summary_b: s(&a[5]),
            similarity: float_arg(&a[6]),
            method: s(&a[7]),
            detector: s(&a[8]),
            created: s(&a[9]),
            status: s(&a[10]),
            verdict_ts: opt_text(&a[11]),
            note: opt_text(&a[12]),
            verdict_hash_a: opt_text(&a[13]),
            verdict_hash_b: opt_text(&a[14]),
        }
        .to_dict(),
        "dedup" => DedupCandidate {
            keeper_id: s(&a[1]),
            keeper_summary: s(&a[2]),
            duplicate_id: s(&a[3]),
            duplicate_summary: s(&a[4]),
            similarity: float_arg(&a[5]),
            method: leak(&a[6]),
        }
        .to_dict(),
        _ => PolaritySkippedPair {
            memory_id_a: s(&a[1]),
            summary_a: s(&a[2]),
            memory_id_b: s(&a[3]),
            summary_b: s(&a[4]),
            similarity: float_arg(&a[5]),
            method: leak(&a[6]),
            detector: leak(&a[7]),
        }
        .to_dict(),
    };
    repr_text(&dict)
}

/// The fields of a row as a tuple, in declaration order.
fn fields(c: &ConflictCandidate) -> PyValue {
    let t = |x: &PyStr| PyValue::Str(x.clone());
    let o = |x: &Option<PyStr>| x.as_ref().map_or(PyValue::None, t);
    PyValue::Tuple(vec![
        t(&c.id),
        t(&c.a_id),
        t(&c.b_id),
        t(&c.summary_a),
        t(&c.summary_b),
        PyValue::Float(c.similarity),
        t(&c.method),
        t(&c.detector),
        t(&c.created),
        t(&c.status),
        o(&c.verdict_ts),
        o(&c.note),
        o(&c.verdict_hash_a),
        o(&c.verdict_hash_b),
    ])
}

fn answer(case: &Value, header: &Value) -> Value {
    let mut out = case.clone();
    let u = header["u"].as_str().expect("u");
    let x = || text_field(case, "x");
    let r = match case["k"].as_str().expect("k") {
        "hash" => body_hash(&x()).map_or_else(|e| err(&e), |h| json!(h)),
        "pid" => pair_id(&text_field(case, "a"), &text_field(case, "b"))
            .map_or_else(|e| err(&e), |h| json!(h)),
        "neg" => json!(has_negation(&x()).expect("an answer")),
        "clause" => {
            let (a, n) = clause_polarity(&x()).expect("an answer");
            json!([sorted(a), sorted(n)])
        }
        "sig" => {
            let sig = body_signals(&x()).expect("an answer");
            json!([
                sig.has_negation,
                sorted(sig.asserted),
                sorted(sig.negated),
                sorted(sig.numbers)
            ])
        }
        "pair" => {
            let a = body_signals(&x()).expect("an answer");
            let b = body_signals(&text_field(case, "y")).expect("an answer");
            json!([
                polarity_flip(&a, &b),
                numeric_divergence(&a.numbers, &b.numbers),
                conflict_signal(&a, &b)
            ])
        }
        "keep" => {
            let a = memory_of(&case["m"][0], u);
            let b = memory_of(&case["m"][1], u);
            match pick_keeper(&a, &b, None, None) {
                Ok((kept, _)) if std::ptr::eq(kept, &a) => json!("ab"),
                Ok(_) => json!("ba"),
                Err(ConflictError::ConflictingPair(det)) => {
                    json!({ "err": "ConflictingPair", "det": det })
                }
                Err(e) => err(&e),
            }
        }
        "from" => {
            let raw = neutral(&case["raw"]);
            let PyValue::Dict(items) = &raw else {
                panic!("a dict")
            };
            match ConflictCandidate::from_dict(items) {
                Ok(row) => repr_text(&fields(&row)),
                Err(e) => err(&e),
            }
        }
        "dict" => to_dict(&case["c"]),
        "scan" => {
            let ms: Vec<Memory> = case["m"]
                .as_array()
                .expect("memories")
                .iter()
                .map(|m| memory_of(m, u))
                .collect();
            let th = (!case["th"].is_null()).then(|| float_of_bits(&case["th"]));
            let (d, f) = scan(&ms, th, &dt(header["now"].as_str().expect("now")));
            out["d"] = d;
            out["f"] = f;
            return out;
        }
        "sweep" => return sweep(case, header),
        other => panic!("unknown case {other}"),
    };
    out["r"] = r;
    out
}

/// `gen_pyre_tree.Lcg`: `s = (MULT * s + INC) mod 2**64`, a draw `s >> 33`.
struct Lcg(u64);

impl Lcg {
    fn draw(&mut self) -> usize {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (self.0 >> 33) as usize
    }
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .expect("a list")
        .iter()
        .map(|s| s.as_str().expect("a string").to_string())
        .collect()
}

/// `gen_conflicts._sweep_set`.
fn sweep_set(lcg: &mut Lcg, case: &Value, u: &str) -> Vec<Memory> {
    let words = strings(&case["words"]);
    let seps = strings(&case["seps"]);
    let updated = strings(&case["updated"]);
    let ids = strings(&case["ids"]);
    let stamps = strings(&case["stamps"]);
    let n = 3 + lcg.draw() % 8;
    let mut body = words[lcg.draw() % words.len()].clone();
    for _ in 1..n {
        body.push_str(&seps[lcg.draw() % seps.len()]);
        body.push_str(&words[lcg.draw() % words.len()]);
    }
    let base: Vec<String> = body.split(' ').map(str::to_string).collect();
    let mut out = Vec::new();
    for _ in 0..2 + lcg.draw() % 11 {
        let mut ws = base.clone();
        for _ in 0..lcg.draw() % 3 {
            // `words[draw] = WORDS[draw]`: Python draws the value first.
            let word = words[lcg.draw() % words.len()].clone();
            let at = lcg.draw() % ws.len();
            ws[at] = word;
        }
        let mut body = ws.join(" ");
        match lcg.draw() % 6 {
            0 => body.push_str(&stamps[0]),
            1 => body.push_str(&stamps[1]),
            _ => {}
        }
        let id = ids[lcg.draw() % ids.len()].clone();
        let up = updated[lcg.draw() % updated.len()].clone();
        let paths: &[&str] = &["p", "q"][..lcg.draw() % 3];
        let last = lcg.draw().is_multiple_of(5).then_some(u);
        out.push(memory(
            unicode::cps(&id),
            unicode::cps(&body),
            &up,
            paths,
            last,
        ));
    }
    out
}

fn sweep(case: &Value, header: &Value) -> Value {
    let u = header["u"].as_str().expect("u");
    let now = dt(header["now"].as_str().expect("now"));
    let ths: Vec<f64> = case["ths"]
        .as_array()
        .expect("ths")
        .iter()
        .map(|t| t.as_f64().expect("a threshold"))
        .collect();
    let mut lcg = Lcg(case["seed"].as_u64().expect("seed"));
    let count = case["count"].as_u64().expect("count") as usize;
    let mut digest = Sha256::new();
    let mut lines = 0;
    for _ in 0..count {
        let ms = sweep_set(&mut lcg, case, u);
        for &th in &ths {
            let (d, f) = scan(&ms, Some(th), &now);
            digest.update(dumps_ascii(&json!([d, f])).as_bytes());
            digest.update(b"\n");
            lines += 1;
        }
    }
    let mut out = case.clone();
    out["lines"] = json!(lines);
    out["sha256"] = json!(common::hex(&digest.finalize()));
    out
}

fn every_case(name: &str, test: &str) {
    let lines = jsonl(name);
    let header = &lines[0];
    assert_eq!(header["statuses"], json!(VALID_STATUSES));
    assert_eq!(header["threshold"], json!(bits(DEFAULT_JACCARD_THRESHOLD)));
    let mut markers = NEGATION_MARKERS.to_vec();
    markers.sort_unstable();
    assert_eq!(header["markers"], json!(markers));
    assert_eq!(
        header["patterns"],
        json!([NEGATION_TOKEN_PATTERN, CLAUSE_SPLIT_PATTERN])
    );
    let in_file = header["cases"].as_u64().expect("cases") as usize;
    assert_eq!(in_file, lines.len() - 1, "{name}: header count");
    let mut ledger = Ledger::new(test, name);
    for (i, case) in lines[1..].iter().enumerate() {
        ledger.case(format!("case {i} ({})", case["k"]), || {
            assert_eq!(answer(case, header), *case, "{name} case {i}");
        });
    }
    ledger.finish(in_file);
}

#[test]
fn conflicts_every_case() {
    every_case("conflicts.jsonl", "conflicts_every_case");
}

#[test]
fn conflicts_unicode_every_case() {
    every_case("conflicts_unicode.jsonl", "conflicts_unicode_every_case");
}
