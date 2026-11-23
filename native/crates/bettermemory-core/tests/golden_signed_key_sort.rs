//! Sorted mapping keys determine the canonical payload and its chain MAC.

mod common;

use bettermemory_core::log::{next_row, LogRow};
use bettermemory_core::pyvalue::PyValue;
use common::parity::Ledger;
use common::{hex, jsonl, written};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const FILE: &str = "signed_key_sort_posix_python.jsonl";
const TS: &str = "2002-03-04T05:06:07.123456Z";

fn fields(case: &Value) -> Vec<(PyValue, PyValue)> {
    let mut value = written::payload(&case["keys"].to_string());
    let PyValue::List(keys) = &mut value else {
        panic!("ordered keys")
    };
    let items = keys
        .drain(..)
        .enumerate()
        .map(|(position, key)| (key, PyValue::int(position as i128)))
        .collect();
    vec![(PyValue::str("metadata"), PyValue::Dict(items))]
}

fn signed(row: &LogRow) -> Value {
    assert_eq!(
        (
            row.seq,
            row.ts.as_str(),
            row.kind.as_str(),
            row.session.as_deref()
        ),
        (2, TS, "search", Some("S"))
    );
    json!({"payload_sha256": hex(&Sha256::digest(row.payload.as_bytes())), "mac": row.mac})
}

#[test]
fn signed_payload_order_and_mac() {
    let rows = jsonl(FILE);
    let previous = LogRow {
        seq: 1,
        mac: rows[0]["previous_mac"].as_str().unwrap().into(),
        ts: String::new(),
        session: None,
        kind: String::new(),
        payload: String::new(),
        prev_mac: String::new(),
    };
    let key: Vec<u8> = (0..32).collect();
    let mut failures = Vec::new();
    for case in &rows[1..] {
        let metadata = PyValue::Dict(fields(case));
        let payload = if case["actor"] == true {
            PyValue::Dict(vec![(PyValue::str("actor"), metadata)])
        } else {
            metadata
        };
        match next_row(Some(&previous), &key, "search", &payload, Some("S"), TS) {
            Ok(row) if signed(&row) == case["out"] => {}
            Err(error) if error.name() == "TypeError" && case["out"]["error"].is_string() => {}
            other => failures.push(format!("{}: {other:?}", case["case"])),
        }
    }
    assert_eq!(rows[0]["count"].as_u64(), Some((rows.len() - 1) as u64));
    Ledger::new("signed_payload_order_and_mac", FILE).settle(rows.len() - 1, &failures);
}

#[cfg(unix)]
#[test]
fn durable_event_and_comparison_error() {
    use bettermemory_core::store::open::open_for_writing;
    use bettermemory_core::store::record_event::{
        record_event, EventCall, EventError, EventTransaction,
    };
    use bettermemory_core::store::write::WriteError;
    use bettermemory_core::unicode::cps;
    use common::{opened, stores::scratch};
    let rows = jsonl(FILE);
    let mut failures = Vec::new();
    for case in &rows[1..] {
        let dir = scratch("signed-key-sort");
        opened::made("created", &dir.0);
        let mut provider = written::clock(&[written::instant(TS)]);
        let mut db =
            open_for_writing(&dir.0.join("memory.sqlite"), &dir.0.join("keys"), &provider).unwrap();
        let call = EventCall {
            kind: &cps("search"),
            session: Some(&cps("S")),
            best_effort: false,
            attempts: 1,
            deadline: None,
        };
        let metadata = fields(case);
        let (fields, actor) = if case["actor"] == true {
            (&[][..], Some(metadata.as_slice()))
        } else {
            (metadata.as_slice(), None)
        };
        let mut logs = Vec::new();
        let result = record_event(
            Ok(EventTransaction::Store(&mut db, &mut provider)),
            &call,
            fields,
            actor,
            &mut |_| vec![],
            &mut logs,
        );
        assert!(logs.is_empty());
        assert!(db.connection().is_autocommit());
        let count: i64 = db
            .connection()
            .query_row("SELECT count(*) FROM log", [], |r| r.get(0))
            .unwrap();
        let actual = match result {
            Ok(Some(row)) => {
                assert_eq!(count, 2);
                let durable: (String, String) = db
                    .connection()
                    .query_row("SELECT payload, mac FROM log WHERE seq=2", [], |r| {
                        Ok((r.get(0)?, r.get(1)?))
                    })
                    .unwrap();
                assert_eq!((&durable.0, &durable.1), (&row.payload, &row.mac));
                signed(&row)
            }
            Err(EventError::Write(WriteError::Refused {
                class: "TypeError",
                text: Some(error),
            })) => {
                assert_eq!(count, 1);
                json!({"error": error})
            }
            other => panic!("unexpected event outcome: {other:?}"),
        };
        if actual != case["out"] {
            failures.push(format!(
                "{}: wanted {}, got {actual}",
                case["case"], case["out"]
            ));
        }
    }
    Ledger::new("durable_event_and_comparison_error", FILE).settle(rows.len() - 1, &failures);
}
