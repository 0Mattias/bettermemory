//! Every case of `log_segments.jsonl`: a chain, a key ring and the
//! caller's key, and the segments and origin check the reference's
//! `chain_trust` gave. Each segment's first seq, name, key, `held`,
//! `foreign` and first row are compared, and the origin's status, seq,
//! reason and signer. `chain_segments` answers the same segments, and
//! where the caller's key is the ring's current one, the crate's older
//! `log::chain_trust` answers their projection.

mod common;

use std::collections::BTreeMap;

use bettermemory_core::log::segments::{chain_segments, chain_trust_with, Segment};
use bettermemory_core::log::{self, Key, KeyRing, LogRow};
use bettermemory_core::unicode;
use common::{assert_count, jsonl, text_field, unhex};
use serde_json::{json, Value};

pub fn row_of(record: &Value) -> LogRow {
    let s = |k: &str| record[k].as_str().expect(k).to_owned();
    LogRow {
        seq: record["seq"].as_i64().expect("seq"),
        ts: s("ts"),
        session: record["session"].as_str().map(str::to_owned),
        kind: s("kind"),
        payload: unicode::to_string(&text_field(record, "payload")).expect("UTF-8 payload"),
        prev_mac: s("prev_mac"),
        mac: s("mac"),
    }
}

fn ring_of(record: &Value) -> KeyRing {
    let file = |v: &Value| v.as_str().map(unhex);
    let mut ring = KeyRing {
        current: file(&record["current"]).map(Key::new),
        origin_file: file(&record["origin"]),
        ..KeyRing::default()
    };
    for entry in record["retired"].as_array().expect("retired") {
        let name = entry[0].as_str().expect("name").to_owned();
        ring.retired
            .insert(name, Key::new(unhex(entry[1].as_str().expect("key"))));
    }
    ring
}

struct Names(BTreeMap<String, String>);

impl Names {
    fn short(&self, name: &[u32]) -> Value {
        match unicode::to_string(name) {
            Some(text) => json!(self.0.get(&text).cloned().unwrap_or(text)),
            None => json!({ "cps": name }),
        }
    }
    fn long(&self, short: &str) -> String {
        match short.strip_prefix('@') {
            Some(letter) => self
                .0
                .iter()
                .find(|(_, v)| v.as_str() == short)
                .unwrap_or_else(|| panic!("no key {letter}"))
                .0
                .clone(),
            None => short.to_owned(),
        }
    }
}

fn segment_json(names: &Names, s: &Segment) -> Value {
    let row_seq = s.first_row.as_ref().map(|r| r.seq);
    let name = names.short(&s.key_fingerprint);
    let mut out = json!({
        "first_seq": s.first_seq,
        "key": s.key.as_ref().map(|k| names.short(&unicode::cps(&log::fingerprint(k.bytes())))),
        "held": s.held(),
        "foreign": s.foreign,
        "first_row_seq": row_seq,
    });
    match name {
        Value::String(_) => out["key_fingerprint"] = name,
        other => out["key_fingerprint_cps"] = other["cps"].clone(),
    }
    out
}

#[test]
fn log_segments_every_case() {
    let lines = jsonl("log_segments.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "log_segments");
    let mut keys: BTreeMap<String, Key> = BTreeMap::new();
    let mut names = Names(BTreeMap::new());
    for (letter, hex) in header["keys"].as_object().expect("keys") {
        let key = Key::new(unhex(hex.as_str().unwrap()));
        names
            .0
            .insert(log::fingerprint(key.bytes()), format!("@{letter}"));
        keys.insert(format!("@{letter}"), key);
    }
    let chains: BTreeMap<&str, Vec<LogRow>> = header["chains"]
        .as_object()
        .expect("chains")
        .iter()
        .map(|(name, rows)| {
            // `chain_trust` reads the rows from seq 1 on, in seq order.
            let mut rows: Vec<LogRow> = rows
                .as_array()
                .expect("rows")
                .iter()
                .map(row_of)
                .filter(|r| r.seq > 0)
                .collect();
            rows.sort_by_key(|r| r.seq);
            (name.as_str(), rows)
        })
        .collect();
    let rings = &header["rings"];
    let callers = &header["callers"];
    let mut failures = Vec::new();
    let mut checked = 0;
    for record in &lines[1..] {
        let case = record["case"].as_str().expect("case");
        let parts: Vec<&str> = case.split('/').collect();
        let rows = &chains[parts[0]];
        let ring = ring_of(&rings[parts[1]]);
        let caller = &callers[parts[2]];
        let current_fp = names.long(caller[0].as_str().unwrap());
        let current_key = caller[1].as_str().map(|k| keys[k].clone());
        let got = match chain_trust_with(
            rows.as_slice(),
            &ring,
            &unicode::cps(&current_fp),
            current_key.as_ref(),
        ) {
            Ok(trust) => {
                let listed = chain_segments(
                    rows.as_slice(),
                    &ring,
                    &unicode::cps(&current_fp),
                    current_key.as_ref(),
                )
                .unwrap();
                assert_eq!(listed.len(), trust.segments.len(), "{case}");
                if current_key.is_some() && current_key == ring.current {
                    let old = log::chain_trust(rows.as_slice(), &ring, &current_fp).unwrap();
                    let projected: Vec<_> = old
                        .segments
                        .iter()
                        .map(|s| (s.first_seq, s.key.clone(), s.foreign))
                        .collect();
                    let whole: Vec<_> = trust
                        .segments
                        .iter()
                        .map(|s| (s.first_seq, s.key.clone(), s.foreign))
                        .collect();
                    assert_eq!(projected, whole, "{case}: the older chain_trust");
                    assert_eq!(old.origin_broken, trust.origin.broken(), "{case}");
                }
                let mut origin = trust.origin.to_dict_json();
                origin["signer"] = match &trust.origin.signer {
                    Some(s) => names.short(&unicode::cps(s)),
                    None => Value::Null,
                };
                json!({
                    "segments": trust.segments.iter().map(|s| segment_json(&names, s)).collect::<Vec<_>>(),
                    "origin": origin,
                })
            }
            Err(e) => json!({ "error": format!("{e:?}") }),
        };
        let mut expected = record.clone();
        expected.as_object_mut().unwrap().remove("case");
        if got != expected {
            failures.push(format!("{case}:\n  expected {expected}\n  got      {got}"));
        }
        checked += 1;
    }
    assert!(
        failures.is_empty(),
        "{} failures:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_count("log_segments.jsonl", checked, lines.len() - 1);
}

trait OriginJson {
    fn to_dict_json(&self) -> Value;
}

impl OriginJson for log::OriginCheck {
    fn to_dict_json(&self) -> Value {
        let mut out = json!({ "status": self.status });
        if let Some(seq) = self.seq {
            out["seq"] = json!(seq);
        }
        if let Some(reason) = self.reason {
            out["reason"] = json!(reason);
        }
        out
    }
}
