//! Product log verification text, JSON bytes and exit selection.
mod common;
use bettermemory_core::cli::log_report::{format_output, Report, ReportError};
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::StoreFile;
use bettermemory_core::{json, unicode};
use common::parity::Ledger;
use sha2::{Digest, Sha256};

fn check_signed(row: &serde_json::Value, name: &str) {
    let temp = common::stores::scratch("log-report");
    let base = &common::stores::bases()["bases"][name];
    common::stores::rebuild(base, &temp.0);
    let store = StoreFile::open(&temp.0.join("memory.sqlite")).unwrap();
    let verified = store
        .log_verify_loading(&mut |s| {
            let meta = s.meta()?;
            let value = |key: &str| meta.iter().find(|(k, _)| k == key).unwrap().1.clone();
            let ring = load_key_ring(&temp.0.join("keys"), &value("store_id")).unwrap();
            Ok((ring, value("key_fingerprint")))
        })
        .unwrap();
    assert_eq!(verified.status, row["status"].as_str().unwrap());
    let mut report = Report::from_verify(&verified, &unicode::cps("notes/é.sqlite"));
    report.add_history(store.history_replaced().unwrap());
    drop(store);
    for (json_out, key) in [(false, "text_sha256"), (true, "json_sha256")] {
        let (code, output) = report.output(json_out).unwrap();
        assert_eq!(code, i32::from(verified.status != "ok"));
        assert_eq!(
            common::hex(&Sha256::digest(output.as_bytes())),
            row[key].as_str().unwrap(),
            "{name} {key}"
        );
    }
}

#[test]
fn cli_log_report_every_case() {
    let name = "cli_log_report.jsonl";
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("cli_log_report_every_case", name);
    let count = rows[0]["cases"].as_u64().unwrap();
    assert!(count > 0);
    ledger.case("count".to_owned(), || {
        assert_eq!(count as usize, rows.len() - 1)
    });
    for (i, row) in rows.iter().enumerate().skip(1) {
        ledger.case(format!("report {i}"), || {
            if let Some(name) = row["base"].as_str() {
                check_signed(row, name);
                return;
            }
            let doc = json::loads(&unicode::cps(row["input"].as_str().unwrap())).unwrap();
            let code = row["code"].as_i64().unwrap() as i32;
            let bytes = row["bytes"].as_bool().unwrap();
            assert_eq!(
                format_output(&doc, bytes, false).unwrap(),
                (code, row["text"].as_str().unwrap().to_owned())
            );
            let result = format_output(&doc, bytes, true);
            if bytes {
                assert_eq!(result, Err(ReportError::BytesLabel));
            } else {
                assert_eq!(
                    result.unwrap(),
                    (code, row["json"].as_str().unwrap().to_owned())
                );
            }
        });
    }
    ledger.finish(rows.len());
}
