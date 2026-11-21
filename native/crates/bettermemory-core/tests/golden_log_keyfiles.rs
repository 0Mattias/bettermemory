//! Every case of `log_keyfiles.jsonl`: the store id check of
//! `log::ring::KeyFiles` (`KeyRing.__post_init__`).

mod common;

use std::path::Path;

use bettermemory_core::log::ring::{BadId, KeyFiles};
use common::{assert_count, jsonl, neutral};
use serde_json::{json, Value};

fn id_case(row: &Value) -> Value {
    match KeyFiles::new(Path::new("keys"), &neutral(&row["id"])) {
        Ok(files) => {
            assert_eq!(
                files.current_path(),
                Path::new("keys").join(format!("{}.key", files.store_id()))
            );
            json!({"ok": true})
        }
        Err(BadId::ValueError(text)) => json!({"raised": "ValueError", "text": text}),
        Err(BadId::Py(e)) => json!({"raised": e.name()}),
    }
}

#[test]
fn log_keyfiles_every_case() {
    let lines = jsonl("log_keyfiles.jsonl");
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(rows.len() as u64));
    let mut failures = Vec::new();
    let mut checked = 0;
    for row in rows {
        assert_eq!(row["part"], "id");
        let (got, want) = (id_case(row), row["out"].clone());
        if got != want {
            failures.push(format!(
                "{}:\n  expected {want}\n  got      {got}",
                row["id"]
            ));
        }
        checked += 1;
    }
    assert!(
        failures.is_empty(),
        "{} failures:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_count("log_keyfiles.jsonl", checked, rows.len());
}
