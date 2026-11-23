//! Signed-store selections from the real Python Windows path methods.
mod common;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::store::key_directory::windows::{
    default_with, resolve_with, wide_units, DirectoryError, Folders,
};
use bettermemory_core::unicode::cps;
use serde_json::json;
use std::collections::BTreeMap;

struct Source<'a> {
    env: BTreeMap<String, PyStr>,
    shell: &'a str,
    calls: Vec<&'static str>,
}

impl Folders for Source<'_> {
    fn environment(&mut self, name: &str) -> Option<PyStr> {
        self.env.get(name).cloned()
    }
    fn local_app_data(&mut self) -> Result<Option<PyStr>, DirectoryError> {
        self.calls.push("CSIDL_LOCAL_APPDATA");
        match self.shell {
            "empty" => Ok(Some(Vec::new())),
            "null" => Ok(None),
            "error" => Err(DirectoryError {
                class: "OSError",
                message: cps("[Errno 5] folder lookup failed"),
                errno: Some(5),
                winerror: None,
            }),
            "value" => Ok(Some(cps("F:/profile/first/../local"))),
            _ => panic!("unknown supplied shell outcome"),
        }
    }
}

#[test]
fn connected_windows_key_directories_match_python() {
    let file = "key_directory_windows.jsonl";
    let rows = common::jsonl(file);
    assert_eq!(rows.len(), 88);
    let mut ledger =
        common::parity::Ledger::new("connected_windows_key_directories_match_python", file);
    for (i, row) in rows.iter().enumerate() {
        ledger.case(i, || {
            let mut source = Source {
                env: serde_json::from_value(row["environment"].clone()).unwrap(),
                shell: row["shell"].as_str().unwrap(),
                calls: Vec::new(),
            };
            let explicit: Option<PyStr> = serde_json::from_value(row["explicit"].clone()).unwrap();
            let answer = if row["default"]==true { default_with(&mut source) }
                else {resolve_with(&mut source,explicit.as_deref())};
            let result = match answer {
                Ok(selected) => json!({"path":selected,"units":wide_units(&selected)}),
                Err(error) => json!({"class":error.class,"message":error.message,"errno":error.errno,"winerror":error.winerror}),
            };
            assert_eq!(result, row["out"],"case {i}");
            assert_eq!(json!(source.calls), row["shell_calls"],"case {i}");
        });
    }
    ledger.finish(rows.len());
}
