//! Every case of `events_kinds.jsonl` (not gated) against the native
//! `events`: the header's tables entry for entry, then each row class,
//! audit answer, kept field set and `_keys_of`. The spelling is the
//! generator's (`tests/contract/gen_events_kinds.py`).

mod common;

use bettermemory_core::events as ev;
use bettermemory_core::pyvalue::PyValue;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn sorted(words: &[&str]) -> Value {
    let mut v = words.to_vec();
    v.sort_unstable();
    json!(v)
}

fn check_tables(h: &Value) {
    assert_eq!(h["acknowledged_suffix"], json!(ev::ACKNOWLEDGED_SUFFIX));
    assert_eq!(h["audit_kinds"], sorted(ev::AUDIT_KINDS));
    assert_eq!(h["audit_statuses"], sorted(ev::AUDIT_STATUSES));
    assert_eq!(h["harness_session_field"], json!(ev::HARNESS_SESSION_FIELD));
    assert_eq!(
        h["harness_stamped_kinds"],
        sorted(ev::HARNESS_STAMPED_KINDS)
    );
    assert_eq!(h["measurement_kinds"], sorted(ev::MEASUREMENT_KINDS));
    assert_eq!(h["overridable_refusals"], json!(ev::OVERRIDABLE_REFUSALS));
    assert_eq!(
        h["override_evidence_kinds"],
        sorted(ev::OVERRIDE_EVIDENCE_KINDS)
    );
    assert_eq!(h["query_hash_prefix"], json!(ev::QUERY_HASH_PREFIX));
    assert_eq!(h["query_preview_chars"], json!(ev::QUERY_PREVIEW_CHARS));
    assert_eq!(h["redacted_text_fields"], sorted(ev::REDACTED_TEXT_FIELDS));
    assert_eq!(
        h["row_classes"],
        json!([
            ["STATE", ev::RowClass::State.value()],
            ["MEASUREMENT", ev::RowClass::Measurement.value()]
        ])
    );
    let row_kinds: Vec<Value> = ev::row_kinds()
        .iter()
        .map(|(c, kinds)| json!([c.value(), sorted(kinds)]))
        .collect();
    assert_eq!(h["row_kinds"], json!(row_kinds));
    let state: Vec<Value> = ev::STATE_FIELDS
        .iter()
        .map(|(kind, kept)| {
            let fields: Vec<Value> = kept
                .iter()
                .map(|(name, keys)| json!([name, keys.map(sorted)]))
                .collect();
            json!([kind, fields])
        })
        .collect();
    assert_eq!(h["state_fields"], json!(state), "STATE_FIELDS");
    let kinds: Vec<&str> = ev::state_kinds().collect();
    assert_eq!(h["state_kinds"], sorted(&kinds));
    assert_eq!(h["state_stamp_fields"], sorted(ev::STATE_STAMP_FIELDS));
    assert_eq!(h["state_write_attempts"], json!(ev::STATE_WRITE_ATTEMPTS));
}

/// `redact_query` of a text the secret strip leaves as it is.
fn redact(text: &[u32]) -> PyValue {
    let utf8: String = text
        .iter()
        .map(|&c| char::from_u32(c).unwrap_or('?'))
        .collect();
    let digest: String = Sha256::digest(utf8.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    let preview: Vec<u32> = text.iter().take(ev::QUERY_PREVIEW_CHARS).copied().collect();
    PyValue::Dict(vec![
        (
            PyValue::str("hash"),
            PyValue::str(&digest[..ev::QUERY_HASH_PREFIX]),
        ),
        (PyValue::str("preview"), PyValue::Str(preview)),
        (PyValue::str("len"), PyValue::int(text.len() as i128)),
    ])
}

fn items(value: PyValue) -> Vec<(PyValue, PyValue)> {
    match value {
        PyValue::Dict(ref items) => items.clone(),
        other => panic!("fields are not a dict: {other:?}"),
    }
}

fn check_row(r: &Value) {
    let kind = || common::text_field(r, "kind");
    match r["k"].as_str().unwrap() {
        "class" => {
            assert_eq!(ev::row_class(&kind()).value(), r["c"].as_str().unwrap());
            assert_eq!(json!(ev::prunable(&kind())), r["p"]);
        }
        "audit" => {
            let fields = items(common::neutral(&r["f"]));
            let got = match ev::is_audit_row(&kind(), &fields) {
                Ok(b) => json!(b),
                Err(ev::EventsError::AttributeError) => json!({"err": "AttributeError"}),
            };
            assert_eq!(got, r["o"], "is_audit_row {r}");
        }
        "state" => {
            let fields = items(common::neutral(&r["f"]));
            match ev::state_fields(&kind(), &fields, &mut redact) {
                Ok(out) => {
                    assert!(r["o"].get("err").is_none(), "expected a raise: {r}");
                    assert!(
                        PyValue::Dict(out) == common::neutral(&r["o"]),
                        "state_fields {r}"
                    );
                }
                Err(ev::EventsError::AttributeError) => {
                    assert_eq!(r["o"], json!({"err": "AttributeError"}), "{r}");
                }
            }
        }
        "keys" => {
            let keys: Vec<&str> = r["keys"]
                .as_array()
                .unwrap()
                .iter()
                .map(|k| k.as_str().unwrap())
                .collect();
            let got = ev::keys_of(&common::neutral(&r["v"]), &keys);
            assert!(got == common::neutral(&r["o"]), "_keys_of {r}");
        }
        other => panic!("unknown row kind {other}"),
    }
}

#[test]
fn events_kinds_every_case() {
    let lines = common::jsonl("events_kinds.jsonl");
    let header = &lines[0];
    assert_eq!(header["k"], "tables");
    check_tables(header);
    let mut checked = 0;
    for r in &lines[1..] {
        check_row(r);
        checked += 1;
    }
    let in_file = header["count"].as_u64().unwrap() as usize;
    assert_eq!(in_file, lines.len() - 1);
    common::assert_count("events_kinds.jsonl", checked, in_file);
}
