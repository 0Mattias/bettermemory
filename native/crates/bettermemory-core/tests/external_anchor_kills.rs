//! Directed regressions for visible state, failure ordering and caller-provided anchors.
use bettermemory_core::anchor::{self, Anchor, AnchorError};
use bettermemory_core::json;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;
use rusqlite::Connection;

fn sample(store: &str, seq: &str) -> Anchor {
    Anchor {
        store_id: cps(store),
        seq: seq.into(),
        mac: cps("m"),
        chain_sha256: cps("d"),
        created_at: vec![0xd800],
    }
}
fn parsed(s: &str) -> PyValue {
    json::loads(&cps(s)).unwrap()
}
fn raised(err: AnchorError, name: &str, text: &str) {
    match err {
        AnchorError::Raised { class, message } => {
            assert_eq!(class, name);
            assert_eq!(message, cps(text));
        }
        e => panic!("{e:?}"),
    }
}
#[test]
fn other_store_skips_binds_and_queries_but_retains_arbitrary_int() {
    let c = Connection::open_in_memory().unwrap();
    let huge = "1267650600228229401496703205376";
    let got =
        anchor::check_anchors(&c, &cps("mine"), &[(vec![0xdcfa], sample("other", huge))]).unwrap();
    assert_eq!(got[0].get("seq"), Some(&PyValue::Int(huge.into())));
    assert_eq!(got[0].get("file"), Some(&PyValue::Str(vec![0xdcfa])));
    assert_eq!(got[0].get("status"), Some(&PyValue::str("other_store")));
    c.execute_batch("CREATE TABLE log(seq INTEGER PRIMARY KEY,mac TEXT)")
        .unwrap();
    raised(
        anchor::check_anchors(&c, &cps("mine"), &[(vec![], sample("mine", huge))]).unwrap_err(),
        "OverflowError",
        "Python int too large to convert to SQLite INTEGER",
    );
    assert!(c.is_autocommit());
}
#[test]
fn empty_and_gap_digests_include_each_row_through_the_requested_seq() {
    let c = Connection::open_in_memory().unwrap();
    assert!(anchor::chain_digests(&c, &[]).unwrap().is_empty());
    c.execute_batch("CREATE TABLE log(seq INTEGER,mac);INSERT INTO log VALUES(2,'m'),(6,7);")
        .unwrap();
    let at =
        anchor::chain_digests(&c, &["6", "0", "1", "3", "2", "6", "7"].map(str::to_owned)).unwrap();
    assert_eq!(
        at.iter().map(|(n, _)| n.as_str()).collect::<Vec<_>>(),
        ["0", "1", "2", "3", "6", "7"]
    );
    let at: std::collections::HashMap<_, _> = at.into_iter().collect();
    assert_eq!(
        at["0"],
        cps("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    );
    assert_eq!(at["1"], at["0"]);
    assert_eq!(at["3"], at["2"]);
    assert_eq!(at["7"], at["6"]);
    assert_ne!(at["2"], at["0"]);
    assert_ne!(at["2"], at["6"]);
}
#[test]
fn ordered_merge_keeps_existing_equal_keys_and_error_prefix() {
    let mut report = parsed(
        r#"{"status":"suspect","problems":[{"seq":"01","problem":"anchor_mismatch","existing":true}],"data":false}"#,
    );
    let entry = |name: &str| {
        parsed(&format!(
            r#"{{"seq":1,"file":"{name}","status":"mismatch","reason":"mac"}}"#
        ))
    };
    anchor::merge_into_report(&mut report, vec![entry("first"), entry("second")]).unwrap();
    let Some(PyValue::List(problems)) = report.get("problems") else {
        panic!("problems")
    };
    assert_eq!(problems[0].get("existing"), Some(&PyValue::Bool(true)));
    assert_eq!(problems[1].get("anchor"), Some(&PyValue::str("first")));
    assert_eq!(problems[2].get("anchor"), Some(&PyValue::str("second")));
    assert_eq!(report.get("status"), Some(&PyValue::str("tampered")));
    let mut bad = parsed(r#"{"status":"ok","problems":5}"#);
    raised(
        anchor::merge_into_report(&mut bad, vec![entry("visible")]).unwrap_err(),
        "TypeError",
        "'int' object is not iterable",
    );
    assert_eq!(
        bad.get("anchors"),
        Some(&PyValue::List(vec![entry("visible")]))
    );
    assert_eq!(bad.get("problems"), Some(&PyValue::int(5)));
    assert_eq!(bad.get("status"), Some(&PyValue::str("ok")));
}
#[test]
fn recursion_in_whole_or_line_remains_a_recursion_error() {
    let deep = format!("{}0{}", "[".repeat(10020), "]".repeat(10020));
    let object = format!("{}0{}", "{\"x\":".repeat(10020), "}".repeat(10020));
    raised(
        anchor::decode_anchors(object.as_bytes(), &[]).unwrap_err(),
        "RecursionError",
        "maximum recursion depth exceeded while decoding a JSON object from a unicode string",
    );
    for raw in [
        deep.clone(),
        format!("\n\n{deep}"),
        format!(
            "{}\n{deep}",
            r#"{"format":"bettermemory.anchor/1","store_id":"s","seq":1,"mac":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","chain_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","created_at":"s"}"#
        ),
    ] {
        assert!(matches!(
            anchor::decode_anchors(raw.as_bytes(), &cps("doc")),
            Err(AnchorError::Raised {
                class: "RecursionError",
                ..
            })
        ));
    }
}

#[test]
fn os_open_reports_programmatic_null_paths_before_any_file_read() {
    let path = std::path::Path::new("normal\0name");
    raised(
        anchor::load_anchors(path).unwrap_err(),
        "AnchorFileError",
        "normal\0name: cannot read an anchor file: open: embedded null character in path",
    );
}
