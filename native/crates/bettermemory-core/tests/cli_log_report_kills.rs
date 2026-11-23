//! Projection of SQL labels into their Python text and JSON representations.
use bettermemory_core::cli::log_report::{Report, ReportError};
use bettermemory_core::log::{ChainReport, ReportValue};
use bettermemory_core::store::fold::{Cell, FoldReport, Label, LogVerify, TableFold};
use bettermemory_core::unicode::cps;

fn sample(labels: Vec<Label>, missing: Vec<Label>) -> Report {
    let status = || vec![("status", ReportValue::Str(cps("ok")))];
    let verified = LogVerify {
        status: "tampered",
        store_id: "example".into(),
        key_fingerprint: "fp".into(),
        chain: ChainReport {
            status: "ok",
            rows: 0,
            segments: vec![],
            problems: vec![],
            head: status(),
            origin: status(),
        },
        problems: vec![],
        fold: FoldReport {
            status: "diverged",
            tables: vec![TableFold {
                table: "memories",
                rows: labels.len(),
                unaccounted: labels,
                missing,
            }],
            membership: vec![],
            problems: vec![],
        },
    };
    Report::from_verify(&verified, &[b'd' as u32, 0xdcff])
}

#[test]
fn scalar_and_composite_sql_bytes_keep_their_python_spelling() {
    let report = sample(
        vec![
            Label::One(Cell::Blob(vec![0, 255])),
            Label::Many(vec![Cell::Text("é".into()), Cell::Blob(b"x".to_vec())]),
        ],
        vec![],
    );
    let (code, text) = report.output(false).unwrap();
    assert_eq!(code, 1);
    assert!(text.contains("  store d\\udcff\n"));
    assert!(text.contains("      unaccounted b'\\x00\\xff'\n"));
    assert!(text.contains("      unaccounted ['é', b'x']\n"));
    assert_eq!(report.output(true), Err(ReportError::BytesLabel));
    assert_eq!(
        ReportError::BytesLabel.to_string(),
        "Object of type bytes is not JSON serializable"
    );
}

#[test]
fn a_string_resembling_a_bytes_label_and_an_integer_stay_serializable() {
    let mut report = sample(
        vec![
            Label::One(Cell::Text("b'x'".into())),
            Label::Many(vec![Cell::Int(8), Cell::Null]),
        ],
        vec![Label::One(Cell::Text("absent".into()))],
    );
    report.add_history(vec![]);
    let (_, text) = report.output(true).unwrap();
    let value: serde_json::Value = serde_json::from_str(&text.replace("\\udcff", "?")).unwrap();
    assert_eq!(
        value["unaccounted_ids"],
        serde_json::json!(["b'x'", [8, null]])
    );
    let table = &value["fold"]["tables"]["memories"];
    assert_eq!(table["unaccounted"], value["unaccounted_ids"]);
    assert_eq!(table["missing"], serde_json::json!(["absent"]));
    assert!(report
        .output(false)
        .unwrap()
        .1
        .contains("      missing absent\n"));
    assert!(value.get("history_replaced").is_none());
    assert!(text.contains("d\\udcff"));
}
