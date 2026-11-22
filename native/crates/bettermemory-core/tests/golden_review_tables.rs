//! Every case of `review_tables.jsonl` and `review_tables_unicode.jsonl`
//! against `review`: the header's constants, payload and shas, the tau
//! overrides, `to_dict`, `RelatedSet`, `to_line`, `related_text`,
//! `related_entry`, `_write_sentences` and the seeded sweep's digest.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::{Confidence, Memory, RecordFields, Source};
use bettermemory_core::review::*;
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn cps(v: &Value) -> PyStr {
    unicode::cps(v.as_str().expect("text"))
}

fn text(t: &[u32]) -> Value {
    json!(unicode::to_string(t).expect("text"))
}

/// A `PyValue` of str, bool, int, list and dict as JSON.
fn val(v: &PyValue) -> Value {
    match v {
        PyValue::Str(s) => text(s),
        PyValue::Bool(b) => json!(b),
        PyValue::Int(n) => json!(n.parse::<i64>().expect("int")),
        PyValue::List(items) => Value::Array(items.iter().map(val).collect()),
        PyValue::Dict(items) => {
            let mut m = serde_json::Map::new();
            for (k, x) in items {
                let PyValue::Str(k) = k else {
                    panic!("str key")
                };
                m.insert(unicode::to_string(k).expect("key"), val(x));
            }
            Value::Object(m)
        }
        other => panic!("no JSON form for {other:?}"),
    }
}

fn body(spec: &Value) -> PyStr {
    let mut out = Vec::new();
    for part in spec.as_array().expect("spec") {
        let piece = cps(&part[0]);
        for _ in 0..part[1].as_u64().expect("times") {
            out.extend_from_slice(&piece);
        }
    }
    out
}

fn tau(t: Option<Tau>) -> Value {
    t.map_or(Value::Null, |t| json!([t.r#unsafe, t.secret]))
}

fn header(h: &Value) {
    assert_eq!(h["related_top"], json!(RELATED_TOP));
    assert_eq!(h["related_cap"], json!(RELATED_CAP));
    assert_eq!(h["related_text_chars"], json!(RELATED_TEXT_CHARS));
    assert_eq!(h["related_constants"], val(&related_constants()));
    assert_eq!(h["roster_text_chars"], json!(ROSTER_TEXT_CHARS));
    assert_eq!(h["tau"], json!([TAU_UNSAFE, TAU_SECRET]));
    assert_eq!(h["tau_now"], tau(current_tau()));
    {
        let g = tau_override(0.25, None);
        let t = g.tau().expect("set");
        assert_eq!(
            h["tau_override_one"],
            json!([t.r#unsafe, t.secret, tau(current_tau())])
        );
    }
    {
        let g = tau_override(0.2, Some(0.7));
        let t = g.tau().expect("set");
        let nested = {
            let _u = tau_unset();
            tau(current_tau())
        };
        assert_eq!(
            h["tau_override_two"],
            json!([t.r#unsafe, t.secret, tau(current_tau())])
        );
        assert_eq!(h["tau_nested"], json!([nested, tau(current_tau())]));
    }
    {
        let _u = tau_unset();
        assert_eq!(h["tau_unset"], tau(current_tau()));
    }
    assert_eq!(h["labels"], json!(LABELS));
    assert_eq!(h["label_choices"], json!(LABEL_CHOICES));
    assert_eq!(
        h["statements"],
        json!([
            UNSAFE_STATEMENT,
            SECRET_STATEMENT,
            STANDING_STATEMENT,
            ROSTER_STATEMENT
        ])
    );
    assert_eq!(
        h["actions"],
        json!([
            ACTION_SECRET,
            ACTION_UNSAFE,
            ACTION_STANDING,
            ACTION_RESTATES,
            ACTION_COMMIT
        ])
    );
    assert_eq!(
        h["detectors"],
        json!([DETECTOR_REVIEW, DETECTOR_REVIEW_DISAGREES])
    );
    assert_eq!(h["link_note"], json!(REVIEW_LINK_NOTE));
    assert_eq!(h["payload"], val(&questions_payload()));
    let canonical = json::review_canonical_json(questions()).expect("canonical");
    assert_eq!(
        h["payload_json"],
        json!(String::from_utf8(canonical).expect("utf8"))
    );
    let ordered =
        json::dumps(&questions_payload(), DumpOptions::UNSORTED_ASCII_COMPACT).expect("dumps");
    assert_eq!(h["payload_ordered"], text(&ordered));
    let ordered =
        json::dumps(&related_constants(), DumpOptions::UNSORTED_ASCII_COMPACT).expect("dumps");
    assert_eq!(h["constants_ordered"], text(&ordered));
    assert_eq!(h["questions_sha256"], json!(questions_sha256()));
    assert_eq!(h["constants_sha256"], json!(constants_sha256()));
    let seals: serde_json::Map<String, Value> = seal_shas()
        .into_iter()
        .map(|(k, v)| (k.to_owned(), json!(v)))
        .collect();
    assert_eq!(h["seal_shas"], Value::Object(seals));
}

fn entry(i: &str, scopes: &[&str], t: &str) -> RelatedEntry {
    RelatedEntry {
        id: unicode::cps(i),
        scopes: scopes.iter().map(|s| unicode::cps(s)).collect(),
        updated: "2026-01-02".to_owned(),
        claim_sized: false,
        text: unicode::cps(t),
    }
}

fn ids(v: &[PyStr]) -> Value {
    Value::Array(v.iter().map(|i| text(i)).collect())
}

fn memory(case: &Value) -> Memory {
    let dt = |t: &str| DateTime::from_isoformat(&unicode::cps(t)).expect("iso");
    Memory {
        fields: RecordFields {
            id: unicode::cps("01J00000000000000000000000"),
            created: dt("2026-01-01T00:00:00+00:00"),
            updated: dt(case["u"].as_str().expect("u")),
            scopes: case["s"].as_array().expect("s").iter().map(cps).collect(),
            confidence: Confidence::MEMBERS[0].1,
            source: Source::MEMBERS[0].1,
            body: body(&case["b"]),
            origin: None,
            actor: None,
            last_verified_at: None,
            category: None,
            verified_paths: Vec::new(),
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

/// Knuth's MMIX generator, as `gen_review_tables._Lcg` draws.
struct Lcg(u64);

impl Lcg {
    fn draw(&mut self, n: usize) -> usize {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        ((self.0 >> 33) % n as u64) as usize
    }
}

fn sweep(case: &Value) -> Value {
    let words: Vec<String> = case["words"]
        .as_array()
        .expect("words")
        .iter()
        .map(|w| w.as_str().expect("w").to_owned())
        .collect();
    let seps: Vec<String> = case["seps"]
        .as_array()
        .expect("seps")
        .iter()
        .map(|w| w.as_str().expect("s").to_owned())
        .collect();
    let mut rng = Lcg(case["seed"].as_u64().expect("seed"));
    let mut h = Sha256::new();
    let count = case["count"].as_u64().expect("count");
    for _ in 0..count {
        let n = 1 + rng.draw(4);
        let content: Vec<String> = (0..n)
            .map(|_| words[rng.draw(words.len())].clone())
            .collect();
        let content = unicode::cps(&content.join(" "));
        let mut b = String::new();
        for _ in 0..1 + rng.draw(260) {
            b.push_str(&words[rng.draw(words.len())]);
            b.push_str(&seps[rng.draw(seps.len())]);
        }
        let b = unicode::cps(&b);
        h.update(
            unicode::to_string(&related_text(&content, &b))
                .expect("utf8")
                .as_bytes(),
        );
        for piece in write_sentences(&b) {
            h.update(unicode::to_string(&piece).expect("utf8").as_bytes());
            h.update([1u8]);
        }
        h.update([0u8]);
    }
    let hex: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
    json!(hex)
}

fn answer(case: &Value) -> Value {
    match case["k"].as_str().expect("k") {
        "to_dict" => {
            let scopes: Vec<&str> = case["s"]
                .as_array()
                .expect("s")
                .iter()
                .map(|s| s.as_str().expect("s"))
                .collect();
            let t = if case["i"] == "x" { "" } else { "t\nu" };
            val(&entry(case["i"].as_str().expect("i"), &scopes, t).to_dict())
        }
        "set" => {
            let base = RelatedSet {
                entries: ["a", "b", "c"]
                    .iter()
                    .map(|n| entry(n, &["s"], &n.to_uppercase()))
                    .collect(),
                report: None,
            };
            let arg: Vec<PyStr> = case["arg"]
                .as_array()
                .expect("arg")
                .iter()
                .map(cps)
                .collect();
            match case["do"].as_str().expect("do") {
                "only" => ids(&base.only(arg.iter().map(Vec::as_slice)).ids()),
                _ => ids(&base
                    .plus(arg.iter().map(|i| {
                        let s = unicode::to_string(i).expect("id");
                        entry(&s, &[], &s)
                    }))
                    .ids()),
            }
        }
        "line" => {
            let a = &case["a"];
            text(
                &RosterLine {
                    id: cps(&a[0]),
                    updated: cps(&a[1]),
                    text: cps(&a[2]),
                }
                .to_line(),
            )
        }
        "text" => text(&related_text(&cps(&case["c"]), &body(&case["b"]))),
        "sent" => ids(&write_sentences(&cps(&case["c"]))),
        "entry" => match related_entry(&cps(&case["c"]), &memory(case)) {
            Ok(e) => val(&e.to_dict()),
            Err(RelatedError::Date(e)) => json!({"e": e.name()}),
            Err(e) => panic!("{e:?}"),
        },
        "sweep" => sweep(case),
        k => panic!("unknown kind {k}"),
    }
}

fn every_case(test: &str, name: &str) {
    let lines = jsonl(name);
    let mut ledger = Ledger::new(test, name);
    for (i, case) in lines.iter().enumerate() {
        ledger.case(format!("case {i} ({})", case["k"]), || {
            if case["k"] == "header" {
                header(case);
                return;
            }
            let mut got = case.clone();
            let key = if case["k"] == "sweep" { "sha256" } else { "r" };
            got[key] = answer(case);
            assert_eq!(got, *case, "{name} case {i}");
        });
    }
    ledger.finish(lines.len());
}

#[test]
fn review_tables_every_case() {
    every_case("review_tables_every_case", "review_tables.jsonl");
}

#[test]
fn review_tables_unicode_every_case() {
    every_case(
        "review_tables_unicode_every_case",
        "review_tables_unicode.jsonl",
    );
}
