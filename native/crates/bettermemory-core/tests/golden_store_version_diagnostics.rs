//! Python exceptions, ordered assertions and SQLite effects on portable diagnostic spellings.
mod common;
use bettermemory_core::store::{
    check_versions,
    open::require_current_writer,
    open_checks, open_checks_at,
    upgrade::{prove_exclusive_at, UpgradeError},
    versions::SqliteDiagnostic,
    write::WriteError,
    DiagnosticError, StoreError, StoreVersionError, Verdict, VersionDiagnostic,
};
use bettermemory_core::unicode::cps;
use common::parity::Ledger;
use rusqlite::{
    hooks::{AuthAction, AuthContext, Authorization},
    Connection,
};
use serde_json::{json, Value};
use std::path::PathBuf;

fn path_for(points: &[u32]) -> PathBuf {
    #[cfg(unix)]
    {
        use bettermemory_core::response_write::{fsencode_as, FsEncoding};
        use std::{ffi::OsString, os::unix::ffi::OsStringExt};
        OsString::from_vec(fsencode_as(points, FsEncoding::SurrogateEscape).unwrap()).into()
    }
    #[cfg(windows)]
    {
        use std::{ffi::OsString, os::windows::ffi::OsStringExt};
        let mut units = Vec::new();
        for &cp in points {
            if let Ok(unit) = u16::try_from(cp) {
                units.push(unit)
            } else {
                units.extend_from_slice(char::from_u32(cp).unwrap().encode_utf16(&mut [0; 2]));
            }
        }
        OsString::from_wide(&units).into()
    }
}

fn sqlite(d: &SqliteDiagnostic) -> Value {
    json!({"class":d.class,"message":d.message,"sqlite_code":d.code,"sqlite_name":d.name(),"key":null,"needed":null,"cause":null})
}
fn version(e: &VersionDiagnostic) -> Value {
    json!({"class":"StoreVersionError","message":e.exact_message,"key":e.key,"needed":e.needed,"sqlite_code":null,"sqlite_name":null,"cause":e.cause.as_deref().map(sqlite)})
}
fn run(row: &Value) {
    let mut c = Connection::open_in_memory().unwrap();
    c.execute_batch("PRAGMA busy_timeout=273; CREATE TABLE trial(value)")
        .unwrap();
    let path = path_for(&serde_json::from_value::<Vec<u32>>(row["path"].clone()).unwrap());
    let mut meta: Vec<(String, String)> = row["meta"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
        .collect();
    if row["missing_meta_table"] != true {
        c.execute_batch("CREATE TABLE meta(key,value)").unwrap();
        for (k, v) in &meta {
            c.execute("INSERT INTO meta VALUES (?,?)", [k, v]).unwrap();
        }
    }
    let (mut result, mut error) = (Value::Null, Value::Null);
    let mut warning_texts = Vec::new();
    match row["op"].as_str().unwrap() {
        "check" => {
            meta.extend([
                ("store_id".into(), "a".repeat(32)),
                ("key_fingerprint".into(), "f".repeat(64)),
                ("schema_version".into(), "1".into()),
            ]);
            match open_checks_at(&c, &meta, &path, "2.4.1") {
                Ok(opened) => {
                    let v = opened.opened.verdict;
                    if let Some(reason) = &opened.read_only_reason {
                        warning_texts.push(reason.clone());
                    }
                    result = json!({"revision":v.revision,"min_reader":v.min_reader,"min_writer":v.min_writer,"read_only":v.read_only.is_some(),"reason":opened.read_only_reason});
                }
                Err(DiagnosticError {
                    error: StoreError::Version(_),
                    version: Some(e),
                }) => error = version(&e),
                Err(e) => panic!("{e:?}"),
            }
        }
        "writer" => {
            c.execute_batch("BEGIN IMMEDIATE").unwrap();
            match require_current_writer(&c, &path) {
                Ok(()) => result = json!(true),
                Err(DiagnosticError {
                    error: WriteError::Version(_),
                    version: Some(e),
                }) => error = version(&e),
                Err(DiagnosticError {
                    error: WriteError::Sqlite(e),
                    version: None,
                }) => error = sqlite(&SqliteDiagnostic::from_error(&e).unwrap()),
                Err(DiagnosticError {
                    error: e @ WriteError::LaterWriter { .. },
                    version: None,
                }) => {
                    error = json!({"class":"OperationalError","message":e.message_points(),"key":null,"needed":null,"sqlite_code":null,"sqlite_name":null,"cause":null})
                }
                Err(e) => panic!("{e:?}"),
            }
            c.execute_batch("ROLLBACK").unwrap();
        }
        "upgrade" | "upgrade-cleanup" => {
            c.execute_batch("BEGIN; INSERT INTO trial VALUES(1)")
                .unwrap();
            if row["op"] == "upgrade-cleanup" {
                c.authorizer(Some(|ctx: AuthContext<'_>| match ctx.action {
                    AuthAction::Pragma {
                        pragma_name: "schema_version",
                        ..
                    } => Authorization::Deny,
                    _ => Authorization::Allow,
                }))
                .unwrap();
            }
            match prove_exclusive_at(&c, &path).unwrap_err() {
                DiagnosticError {
                    error: UpgradeError::Version(_),
                    version: Some(e),
                } => error = version(&e),
                DiagnosticError {
                    error: UpgradeError::Sqlite { error: exc, .. },
                    version: None,
                } => error = sqlite(&SqliteDiagnostic::from_error(&exc).unwrap()),
                other => panic!("unexpected diagnostic {other:?}"),
            }
            c.authorizer(None::<fn(AuthContext<'_>) -> Authorization>)
                .unwrap();
        }
        op => panic!("unexpected operation {op}"),
    }
    assert_eq!(result, row["result"]);
    assert_eq!(error, row["error"]);
    // This reader exposes the reason the Python open logs on its first occurrence.
    assert_eq!(json!(warning_texts), row["warnings"]);
    assert_eq!(json!(!c.is_autocommit()), row["in_transaction"]);
    for (column, sql) in [
        ("rows", "SELECT count(*) FROM trial"),
        ("busy_timeout", "PRAGMA busy_timeout"),
        ("query_only", "PRAGMA query_only"),
    ] {
        let n: i64 = c.query_row(sql, [], |r| r.get(0)).unwrap();
        assert_eq!(json!(n), row[column]);
    }
}
#[test]
fn version_diagnostics_match_real_sqlite() {
    let name = "store_version_diagnostics.jsonl";
    let rows = common::jsonl(name);
    assert_eq!(rows[0]["fixture"], "store_version_diagnostics");
    let mut ledger = Ledger::new("version_diagnostics_match_real_sqlite", name);
    for (i, row) in rows[1..].iter().enumerate() {
        ledger.case(i, || run(row));
    }
    ledger.finish(rows.len() - 1);
}
#[test]
fn scalar_legacy_interfaces_keep_their_original_field_types() {
    let bad = vec![("min_writer_revision".into(), "bad".into())];
    let StoreVersionError {
        message,
        key,
        needed,
    } = check_versions(&bad, "plain", "1").unwrap_err();
    let expected = StoreVersionError {
        message: message.clone(),
        key: Some("min_writer_revision"),
        needed: None,
    };
    assert_eq!(
        (message, key, needed),
        (expected.message, expected.key, expected.needed)
    );
    let Verdict {
        read_only,
        revision,
        min_reader,
        min_writer,
    } = check_versions(&[], "plain", "1").unwrap();
    assert_eq!(
        Verdict {
            read_only,
            revision,
            min_reader,
            min_writer
        },
        Verdict {
            read_only: None,
            revision: 0,
            min_reader: 0,
            min_writer: 0
        }
    );
    let mut meta = vec![
        ("store_id".into(), "a".repeat(32)),
        ("key_fingerprint".into(), "f".repeat(64)),
        ("schema_version".into(), "1".into()),
        ("min_writer_revision".into(), "99".into()),
    ];
    let v = open_checks(&meta, "plain", "1").unwrap().verdict;
    let reason: &Option<String> = &v.read_only;
    let exact = bettermemory_core::store::check_versions_points(&meta, &cps("plain"), "1").unwrap();
    assert_eq!(reason.as_deref().map(cps), exact.read_only_points);
    meta[3].1 = "x".into();
    assert!(matches!(
        open_checks(&meta, "plain", "1"),
        Err(StoreError::Version(_))
    ));
}
