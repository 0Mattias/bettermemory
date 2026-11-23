//! Metadata and revision refusals through the live connection entry point.
mod common;

use bettermemory_core::store::{open_checks_at_points, OpenCheckError};
use common::parity::Ledger;
use rusqlite::Connection;
use serde_json::{json, Value};

fn compare(row: &Value) {
    let db = Connection::open_in_memory().unwrap();
    db.execute_batch("BEGIN").unwrap();
    let metadata: Vec<(String, String)> =
        serde_json::from_value::<std::collections::BTreeMap<_, _>>(row["meta"].clone())
            .unwrap()
            .into_iter()
            .collect();
    let path: Vec<u32> = serde_json::from_value(row["path_cps"].clone()).unwrap();
    let result = open_checks_at_points(&db, &metadata, &path, "2.4.1");
    assert_eq!(json!(result.is_ok()), row["reached_keys"]);
    let error = match result {
        Ok(checked) => {
            assert_eq!(json!(checked.opened.store_id), row["keyring_id"]);
            assert_eq!(
                json!(checked.opened.key_fingerprint),
                row["meta"]["key_fingerprint"]
            );
            assert_eq!(json!(checked.read_only_reason), row["read_only_reason"]);
            Value::Null
        }
        Err(OpenCheckError::ValueError(message)) => json!({
            "class":"ValueError", "message_cps":message, "key":null, "needed":null,
        }),
        Err(OpenCheckError::Version(e)) => json!({
            "class":"StoreVersionError", "message_cps":e.exact_message, "key":e.key, "needed":e.needed,
        }),
        Err(other) => panic!("unexpected open error {other:?}"),
    };
    assert_eq!(error, row["error"]);
    let query_only: i64 = db.query_row("PRAGMA query_only", [], |r| r.get(0)).unwrap();
    assert_eq!(json!(query_only), row["query_only"]);
    assert_eq!(json!(!db.is_autocommit()), row["in_transaction"]);
}

#[test]
fn metadata_refusals_match_the_connected_python_store() {
    let name = "store_open_reasons.jsonl";
    let rows = common::jsonl(name);
    assert_eq!(rows[0]["fixture"], "store_open_reasons");
    let mut ledger = Ledger::new("metadata_refusals_match_the_connected_python_store", name);
    for (index, row) in rows[1..].iter().enumerate() {
        ledger.case(index, || compare(row));
    }
    ledger.finish(rows.len() - 1);
}
