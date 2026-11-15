//! Every case of `json_edges.jsonl` (Python 3.13's results): dicts with
//! NaN keys through every JSON form, and how deep a value each JSON call
//! site writes or reads: at the reference's deepest depth the same output
//! (its length and SHA-256) or report, one deeper the same exception.

mod common;

use bettermemory_core::json;
use bettermemory_core::log::{self, KeyRing, LogRow};
use bettermemory_core::pyvalue::{self, PyError, PyValue};
use bettermemory_core::unicode;
use common::{assert_count, json_form, json_form_out, jsonl, neutral, unhex};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const TS: &str = "2026-01-02T03:04:05.000Z";

fn nested(shape: &str, depth: usize) -> PyValue {
    let mut value = PyValue::int(0);
    for _ in 0..depth {
        value = match shape {
            "list" => PyValue::List(vec![value]),
            _ => PyValue::Dict(vec![(PyValue::str("a"), value)]),
        };
    }
    value
}

fn nested_text(shape: &str, depth: usize) -> String {
    match shape {
        "list" => format!("{}0{}", "[".repeat(depth), "]".repeat(depth)),
        _ => format!("{}0{}", "{\"a\":".repeat(depth), "}".repeat(depth)),
    }
}

/// The two-key chain of `log_keyring.jsonl`: A signs rows 1-3, the rekey
/// row (`rekey`, or the canonical one) is signed by B, B signs 5-6.
fn two_key(a: &[u8], b: &[u8], rekey: Option<String>) -> Vec<LogRow> {
    let canonical = json::canonical_payload(&log::rekey_payload(a, b)).expect("rekey text");
    let specs: [(&[u8], &str, String); 6] = [
        (a, log::STORE_CREATED, "{}".into()),
        (a, "note", "{\"n\":1}".into()),
        (a, "note", "{\"n\":2}".into()),
        (b, log::REKEY, rekey.unwrap_or(canonical)),
        (b, "note", "{\"n\":3}".into()),
        (b, "note", "{\"n\":4}".into()),
    ];
    let mut rows: Vec<LogRow> = Vec::new();
    for (i, (key, kind, payload)) in specs.into_iter().enumerate() {
        let seq = i as i64 + 1;
        let prev = rows
            .last()
            .map_or(log::GENESIS_MAC.to_owned(), |r| r.mac.clone());
        let mac = log::compute_mac(key, seq, TS, None, kind, &payload, &prev);
        rows.push(LogRow {
            seq,
            ts: TS.into(),
            session: None,
            kind: kind.into(),
            payload,
            prev_mac: prev,
            mac,
        });
    }
    rows
}

struct Keys {
    a: Vec<u8>,
    b: Vec<u8>,
}

impl Keys {
    fn verify(
        &self,
        head: Option<String>,
        origin: Option<String>,
        rekey: Option<String>,
    ) -> Result<Value, PyError> {
        let rows = two_key(&self.a, &self.b, rekey);
        let mut ring = KeyRing {
            current: Some(log::Key::new(self.b.clone())),
            head_file: head.map(String::into_bytes),
            origin_file: origin.map(String::into_bytes),
            ..KeyRing::default()
        };
        ring.retire(log::Key::new(self.a.clone()));
        let report = log::verify_chain(&rows, &ring, &log::fingerprint(&self.b))?;
        let head = report
            .head
            .iter()
            .find(|(k, _)| *k == "status")
            .map(|(_, v)| match v {
                log::ReportValue::Str(s) => unicode::to_string(s).expect("a status"),
                other => panic!("head status {other:?}"),
            })
            .expect("a head status");
        Ok(json!({ "status": report.status, "head": head }))
    }
}

fn digest(bytes: &[u8]) -> Value {
    let sha: String = Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    json!({ "len": bytes.len(), "sha256": sha })
}

/// The call of `site` at `depth`: Ok(what the fixture records at the
/// deepest depth, or null where it records nothing), or the exception.
fn call(keys: &Keys, site: &str, shape: &str, depth: usize) -> Result<Value, PyError> {
    if let Some(form) = site.strip_prefix("dumps ") {
        if form == "log_payload_in_dict" {
            let payload = PyValue::Dict(vec![(PyValue::str("p"), nested(shape, depth))]);
            return Ok(digest(json::canonical_payload(&payload)?.as_bytes()));
        }
        return Ok(digest(&json_form_out(form, &nested(shape, depth))?.bytes()));
    }
    match site {
        "loads" => json::loads(&unicode::cps(&nested_text(shape, depth))).map(|_| Value::Null),
        "repr" => {
            let text = pyvalue::repr(&nested(shape, depth))?;
            Ok(digest(unicode::to_string(&text).expect("ASCII").as_bytes()))
        }
        "verify head_mac" => keys.verify(
            Some(format!(
                "{{\"seq\": 6, \"mac\": {}}}",
                nested_text(shape, depth)
            )),
            None,
            None,
        ),
        "verify origin_extra" => keys.verify(
            None,
            Some(format!(
                "{{\"seq\": 1, \"mac\": \"x\", \"extra\": {}}}",
                nested_text(shape, depth)
            )),
            None,
        ),
        "verify rekey_extra" => {
            let canonical =
                json::canonical_payload(&log::rekey_payload(&keys.a, &keys.b)).expect("rekey text");
            let text = format!(
                "{},\"x\":{}}}",
                &canonical[..canonical.len() - 1],
                nested_text(shape, depth)
            );
            keys.verify(None, None, Some(text))
        }
        other => panic!("site {other} is not ported"),
    }
}

fn outcome(result: &Result<Value, PyError>) -> Value {
    match result {
        Ok(_) => json!({ "ok": true }),
        Err(e) => json!({ "error": e.name() }),
    }
}

#[test]
fn json_edges_every_case() {
    let lines = jsonl("json_edges.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "json_edges");
    assert_eq!(header["python"], "3.13");
    let ring = jsonl("log_keyring.jsonl");
    let keys = Keys {
        a: unhex(ring[0]["keys"]["A"].as_str().unwrap()),
        b: unhex(ring[0]["keys"]["B"].as_str().unwrap()),
    };
    let (mut sorts, mut depths, mut cells) = (0, 0, 0);
    let mut failures = Vec::new();
    for record in &lines[1..] {
        if let Some(name) = record.get("sort") {
            let value = neutral(&record["input"]);
            for (form, expected) in record["out"].as_object().expect("out") {
                let got = json_form(form, &value).unwrap_or_else(|e| json!({ "error": e.name() }));
                if &got != expected {
                    failures.push(format!(
                        "sort {name} / {form}: expected {expected}, got {got}"
                    ));
                }
                cells += 1;
            }
            sorts += 1;
            continue;
        }
        let site = record["site"].as_str().expect("site");
        let shape = record["shape"].as_str().expect("shape");
        let what = format!("{site} ({shape})");
        if let Some(limit) = record.get("python_recursion") {
            // A site that recurses in Python: below the recursion limit by
            // more than any caller holds it is taken, at the limit refused.
            for (depth, expected) in [("ok_at", "at_ok"), ("fails_at", "at_fails")] {
                let d = limit[depth].as_u64().expect("a depth") as usize;
                let got = outcome(&call(&keys, site, shape, d));
                if got != limit[expected] {
                    failures.push(format!(
                        "{what} at {d}: expected {}, got {got}",
                        limit[expected]
                    ));
                }
                cells += 1;
            }
            depths += 1;
            continue;
        }
        let max_ok = record["max_ok"].as_u64().expect("max_ok") as usize;
        assert_eq!(record["first_failing"].as_u64(), Some(max_ok as u64 + 1));
        match call(&keys, site, shape, max_ok) {
            Ok(got) => {
                let expected = record.get("at_max").cloned().unwrap_or(Value::Null);
                if got != expected {
                    failures.push(format!(
                        "{what} at {max_ok}: expected {expected}, got {got}"
                    ));
                }
            }
            Err(e) => failures.push(format!("{what} at {max_ok}: raised {}", e.name())),
        }
        let got = outcome(&call(&keys, site, shape, max_ok + 1));
        if got != record["at_first_failing"] {
            failures.push(format!(
                "{what} at {}: expected {}, got {got}",
                max_ok + 1,
                record["at_first_failing"]
            ));
        }
        cells += 2;
        depths += 1;
    }
    eprintln!("json_edges: {sorts} sort cases, {depths} depth cases, {cells} cells");
    assert!(
        failures.is_empty(),
        "{} failures:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_count("json_edges.jsonl", sorts + depths, lines.len() - 1);
}
