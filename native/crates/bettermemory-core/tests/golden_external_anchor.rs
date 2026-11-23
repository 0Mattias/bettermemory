//! Public external-anchor entry points on source-generated inputs and a real signed store.
mod common;

use bettermemory_core::anchor::{self, Anchor, AnchorError};
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::StoreFile;
use bettermemory_core::unicode::{cps, to_string};
use common::parity::Ledger;
use serde_json::Value;

fn parse(raw: &str) -> PyValue {
    json::loads(&cps(raw)).unwrap()
}
fn string(value: &Value) -> &str {
    value.as_str().unwrap()
}
fn text(s: &Value) -> PyStr {
    match parse(string(s)) {
        PyValue::Str(ref t) => t.clone(),
        _ => panic!("text"),
    }
}
fn wire(value: &PyValue) -> String {
    to_string(
        &json::dumps(
            value,
            DumpOptions {
                sort_keys: false,
                ..DumpOptions::SORTED_ASCII_COMPACT
            },
        )
        .unwrap(),
    )
    .unwrap()
}
fn outcome(result: Result<PyValue, AnchorError>) -> PyValue {
    PyValue::List(match result {
        Ok(v) => vec![PyValue::str("ok"), v],
        Err(AnchorError::Raised { class, message }) => vec![
            PyValue::str("error"),
            PyValue::str(class),
            PyValue::Str(message),
        ],
        Err(e) => panic!("unexpected error {e:?}"),
    })
}
fn list(values: Vec<Anchor>) -> PyValue {
    PyValue::List(values.iter().map(Anchor::to_dict).collect())
}

#[test]
fn parsed_files_match_reference() {
    let file = "external_anchor.jsonl";
    let rows = common::jsonl(file);
    let mut ledger = Ledger::new("parsed_files_match_reference", file);
    for (i, r) in rows.iter().enumerate() {
        ledger.case(i, || {
            let result = match string(&r["op"]) {
                "parse" => anchor::parse_anchor(&parse(string(&r["raw"])), &text(&r["place"]))
                    .map(|a| a.to_dict()),
                "decode" => anchor::decode_anchors(&common::unhex(string(&r["raw"])), &cps("FILE"))
                    .map(list),
                unknown => panic!("unknown {unknown}"),
            };
            assert_eq!(wire(&outcome(result)), string(&r["expected"]));
        });
    }
    ledger.finish(rows.len());
}

#[cfg(unix)]
#[test]
fn posix_files_match_reference() {
    use std::{
        ffi::OsString,
        fs,
        os::unix::{ffi::OsStringExt, fs::symlink},
        process::Command,
    };
    let file = "external_anchor_files_posix.jsonl";
    let rows = common::jsonl(file);
    let scratch = common::stores::scratch("anchor-files");
    let root = &scratch.0;
    let portable = common::jsonl("external_anchor.jsonl");
    fs::write(root.join("regular"), string(&portable[0]["raw"])).unwrap();
    symlink("regular", root.join("linked")).unwrap();
    assert!(Command::new("mkfifo")
        .arg(root.join("pipe"))
        .status()
        .unwrap()
        .success());
    symlink("pipe", root.join("pipe-link")).unwrap();
    let mut ledger = Ledger::new("posix_files_match_reference", file);
    for r in &rows {
        ledger.case(string(&r["name"]), || {
            let name = text(&r["name"]);
            let mut bytes = Vec::new();
            for c in name {
                if (0xdc80..=0xdcff).contains(&c) {
                    bytes.push((c - 0xdc00) as u8);
                } else {
                    let mut buf = [0; 4];
                    bytes.extend_from_slice(
                        char::from_u32(c).unwrap().encode_utf8(&mut buf).as_bytes(),
                    );
                }
            }
            let path = root.join(OsString::from_vec(bytes));
            let result = wire(&outcome(anchor::load_anchors(&path).map(list)));
            let expected = match r.get("error") {
                Some(class) => {
                    let message: PyStr = serde_json::from_value(r["message"].clone()).unwrap();
                    wire(&PyValue::List(vec![
                        PyValue::str("error"),
                        PyValue::str(string(class)),
                        PyValue::Str(message),
                    ]))
                }
                None => string(&r["expected"]).to_owned(),
            };
            assert_eq!(
                result.replace(&format!("{}/", root.display()), ""),
                expected
            );
        });
    }
    ledger.finish(rows.len());
}

#[test]
fn historical_signed_store_matches_reference() {
    let file = "external_anchor_signed.jsonl";
    let rows = common::jsonl(file);
    let scratch = common::stores::scratch("anchor-signed");
    common::stores::rebuild(&common::stores::bases()["bases"]["intact"], &scratch.0);
    let filename = scratch.0.join("memory.sqlite");
    let store = StoreFile::open(&filename).unwrap();
    let conn = store.connection();
    let before = conn.total_changes();
    let mut ledger = Ledger::new("historical_signed_store_matches_reference", file);
    for r in &rows {
        ledger.case(string(&r["results"]),||{
        let anchors:Vec<_>=r["anchors"].as_array().unwrap().iter().map(|pair|{
            let raw=parse(string(&pair[1]));
            let Some(PyValue::Int(seq))=raw.get("seq") else {panic!("fixture sequence")};
            let a=if seq.parse::<i64>().is_ok(){
                anchor::parse_anchor(&raw,&cps("fixture source")).unwrap()
            }else{
                // A direct foreign Anchor can carry a Python integer outside SQLite's range.
                let value=|key:&str|match raw.get(key){Some(PyValue::Str(s))=>s.clone(),_=>panic!("missing {key}")};
                Anchor{seq:seq.clone(),store_id:value("store_id"),mac:value("mac"),chain_sha256:value("chain_sha256"),created_at:value("created_at")}
            };
            (text(&pair[0]),a)
        }).collect();
        let result=anchor::check_anchors(conn,&cps(string(&r["store_id"])),&anchors).unwrap();
        assert_eq!(wire(&PyValue::List(result.clone())),string(&r["results"]));
        let mut report=parse(r#"{"status":"ok","existing":null,"problems":[{"seq":1,"problem":"anchor_mismatch","detail":"already seen"}]}"#);
        anchor::merge_into_report(&mut report,result).unwrap();
        assert_eq!(wire(&report),string(&r["report"]));
        assert!(conn.is_autocommit());
    });
    }
    ledger.finish(rows.len());
    assert_eq!(conn.total_changes(), before);
}
