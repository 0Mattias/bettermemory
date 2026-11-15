//! Every row of `log_chain.jsonl` (its payload text from the neutral
//! payload, its MAC, the chain rebuilt row by row) and every variant's
//! `verify_chain` report, field for field.

mod common;

use bettermemory_core::json;
use bettermemory_core::log::{self, ChainReport, KeyRing, LogRow, ReportDict, ReportValue};
use bettermemory_core::unicode;
use common::{assert_count, hex, jsonl, neutral, text_field, unhex};
use serde_json::{json, Map, Value};

fn row_of(record: &Value) -> LogRow {
    let s = |k: &str| record[k].as_str().expect(k).to_owned();
    LogRow {
        seq: record["seq"].as_i64().unwrap(),
        ts: s("ts"),
        session: record["session"].as_str().map(str::to_owned),
        kind: s("kind"),
        payload: unicode::to_string(&text_field(record, "payload")).expect("stored text is UTF-8"),
        prev_mac: s("prev_mac"),
        mac: s("mac"),
    }
}

fn report_value(v: &ReportValue) -> Value {
    match v {
        ReportValue::Null => Value::Null,
        ReportValue::Int(i) => json!(i),
        ReportValue::BigInt(text) => panic!("no fixture head holds a seq past i64: {text}"),
        ReportValue::Str(s) => {
            json!(unicode::to_string(s).expect("no surrogate in a fixture report"))
        }
    }
}

fn dict(d: &ReportDict) -> Value {
    let mut m = Map::new();
    for (k, v) in d {
        assert!(
            m.insert((*k).to_owned(), report_value(v)).is_none(),
            "repeated key {k}"
        );
    }
    Value::Object(m)
}

fn report_json(r: &ChainReport) -> Value {
    json!({
        "status": r.status,
        "rows": r.rows,
        "segments": r.segments.iter().map(dict).collect::<Vec<_>>(),
        "problems": r.problems.iter().map(dict).collect::<Vec<_>>(),
        "head": dict(&r.head),
        "origin": dict(&r.origin),
    })
}

#[test]
fn log_chain_every_row_and_variant() {
    let lines = jsonl("log_chain.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "log_chain");
    assert_eq!(header["genesis_mac"], log::GENESIS_MAC);
    let keys = &header["keys"];
    let key_a = unhex(keys["A"].as_str().unwrap());
    let key_b = unhex(keys["B"].as_str().unwrap());
    assert_eq!(log::fingerprint(&key_a), keys["A_fingerprint"]);
    assert_eq!(log::fingerprint(&key_b), keys["B_fingerprint"]);
    let fp_b = log::fingerprint(&key_b);

    let row_records: Vec<&Value> = lines[1..]
        .iter()
        .filter(|r| r.get("row").is_some())
        .collect();
    let variant_records: Vec<&Value> = lines[1..]
        .iter()
        .filter(|r| r.get("variant").is_some())
        .collect();
    assert_eq!(row_records.len() + variant_records.len(), lines.len() - 1);

    let mut failures = Vec::new();
    let mut rows: Vec<LogRow> = Vec::new();
    let mut rows_checked = 0;
    for record in &row_records {
        let stored = row_of(&record["row"]);
        let key = match record["key"].as_str().unwrap() {
            "A" => &key_a,
            "B" => &key_b,
            other => panic!("key {other}"),
        };
        let payload = neutral(&record["payload_input"]);
        let what = format!("row {} ({})", stored.seq, stored.kind);
        match json::canonical_payload(&payload) {
            Ok(text) => {
                if hex(text.as_bytes()) != record["row"]["payload_hex"] {
                    failures.push(format!("{what}: payload text {text:?}"));
                }
            }
            Err(e) => failures.push(format!("{what}: payload raised {}", e.name())),
        }
        let mac = log::compute_mac(
            key,
            stored.seq,
            &stored.ts,
            stored.session.as_deref(),
            &stored.kind,
            &stored.payload,
            &stored.prev_mac,
        );
        if mac != stored.mac {
            failures.push(format!("{what}: mac {mac}"));
        }
        if stored.kind == log::REKEY && log::rekey_payload(&key_a, &key_b) != payload {
            failures.push(format!("{what}: rekey payload"));
        }
        // The chain as `append_row` writes it, from the neutral payload.
        match log::next_row(
            rows.last(),
            key,
            &stored.kind,
            &payload,
            stored.session.as_deref(),
            &stored.ts,
        ) {
            Ok(built) if built == stored => {}
            Ok(built) => failures.push(format!("{what}: rebuilt row differs: {built:?}")),
            Err(e) => failures.push(format!("{what}: rebuilding raised {}", e.name())),
        }
        rows.push(stored);
        rows_checked += 1;
    }
    let last = rows.last().unwrap();
    assert_eq!(header["head"], json!({ "seq": last.seq, "mac": last.mac }));

    let mut variants_checked = 0;
    for record in &variant_records {
        let name = record["variant"].as_str().unwrap();
        let mut variant = rows.clone();
        if let Some(edit) = record.get("edit") {
            let index = edit[0].as_u64().unwrap() as usize;
            let value = &edit[2];
            let text = || value.as_str().unwrap().to_owned();
            let row = &mut variant[index];
            match edit[1].as_str().unwrap() {
                "payload" => row.payload = text(),
                "ts" => row.ts = text(),
                "session" => row.session = value.as_str().map(str::to_owned),
                "kind" => row.kind = text(),
                "mac" => row.mac = text(),
                "prev_mac" => row.prev_mac = text(),
                other => panic!("edit of {other}"),
            }
        }
        if let Some(drop) = record.get("drop") {
            let drop: Vec<usize> = drop
                .as_array()
                .unwrap()
                .iter()
                .map(|i| i.as_u64().unwrap() as usize)
                .collect();
            variant = variant
                .into_iter()
                .enumerate()
                .filter(|(i, _)| !drop.contains(i))
                .map(|(_, r)| r)
                .collect();
        }
        let mut ring = KeyRing {
            current: Some(key_b.clone().into()),
            ..KeyRing::default()
        };
        ring.retire(key_a.clone().into());
        if let Some(head) = record["head"].as_object() {
            let text = log::head_file_text(
                head["seq"].as_i64().unwrap(),
                head["mac"].as_str().unwrap(),
                "",
            );
            ring.head_file = Some(text.into_bytes());
        }
        let got = match log::verify_chain(&variant, &ring, &fp_b) {
            Ok(report) => report_json(&report),
            Err(e) => json!({ "error": e.name() }),
        };
        if got != record["report"] {
            failures.push(format!(
                "variant {name}:\n  expected {}\n  got      {got}",
                record["report"]
            ));
        }
        variants_checked += 1;
    }
    assert!(
        failures.is_empty(),
        "{} failures:\n{}",
        failures.len(),
        failures.join("\n")
    );
    eprintln!("log_chain: {rows_checked} rows, {variants_checked} variants");
    assert_eq!(rows_checked, row_records.len());
    assert_eq!(variants_checked, 17);
    assert_count(
        "log_chain.jsonl",
        rows_checked + variants_checked,
        lines.len() - 1,
    );
}
