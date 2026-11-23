//! Real store open, mutation, event, digest, and upgrade consumers.
mod common;
use bettermemory_core::{
    fsutil::fsdecode,
    pyvalue::PyValue,
    store::{
        create::{create, System},
        open::{
            open_for_writing, open_for_writing_diagnostic, open_for_writing_with, Boundary,
            DigestOutcome, NotPorted, OpenError, WritableStore,
        },
        record_event::{record_event, EventCall, EventError, EventTransaction},
        upgrade::{prove_exclusive_at, UpgradeError},
        write::WriteError,
        DiagnosticError, StoreError, StoreFile,
    },
    unicode::cps,
};
use common::stores::scratch;
use rusqlite::Connection;
use std::{ffi::OsStr, path::Path};

fn field(conn: &Connection, name: &str) -> i64 {
    conn.query_row(name, [], |r| r.get(0)).unwrap()
}
fn revision(conn: &Connection, key: &str, value: &str) {
    assert_eq!(
        conn.execute("UPDATE meta SET value=?1 WHERE key=?2", [value, key])
            .unwrap(),
        1
    );
}
fn event(
    store: &mut WritableStore,
    provider: &mut System,
    best_effort: bool,
) -> (
    Result<Option<bettermemory_core::log::LogRow>, EventError>,
    Vec<bettermemory_core::store::record_event::Logged>,
) {
    let mut logs = Vec::new();
    let kind = cps("diagnostic");
    let call = EventCall {
        kind: &kind,
        session: None,
        best_effort,
        attempts: 1,
        deadline: None,
    };
    let out = record_event(
        Ok(EventTransaction::Store(store, provider)),
        &call,
        &[],
        None,
        &mut |_| Vec::new(),
        &mut logs,
    );
    (out, logs)
}
fn exercise(name: &OsStr) {
    let root = scratch("version-diagnostics");
    let dir = root.0.join(name);
    std::fs::create_dir(&dir).unwrap();
    let path = dir.join("memory.sqlite");
    let keys = root.0.join("keys");
    let mut clock = System {
        engine_version: "2.4.1".into(),
    };
    create(&path, &keys, &mut clock).unwrap();
    let path_points = fsdecode(path.as_os_str());
    let mut bad = path_points.clone();
    bad.extend(cps(" carries a min_writer_revision that is not a whole number ('bad'); the store is not opened: set meta's min_writer_revision back to a whole number, or restore the store from a backup taken before the edit"));
    let conn = Connection::open(&path).unwrap();
    let mut current = open_for_writing(&path, &keys, &clock).unwrap();
    let (accepted, logs) = event(&mut current, &mut clock, false);
    assert!(accepted.unwrap().is_some());
    assert!(logs.is_empty());
    current.close().unwrap();
    let initial = field(&conn, "SELECT count(*) FROM log");
    assert_eq!(initial, 2);
    revision(&conn, "min_writer_revision", "bad");
    {
        let file = StoreFile::open(&path).unwrap();
        let meta = file.meta().unwrap();
        let Err(DiagnosticError {
            error: StoreError::Version(_),
            version: Some(e),
        }) = file.check_opened(&meta, "2.4.1")
        else {
            panic!("expected invalid revision")
        };
        assert_eq!(e.exact_message, bad);
        assert_eq!((e.key, e.needed), (Some("min_writer_revision"), None));
        assert_eq!(field(file.connection(), "PRAGMA query_only"), 0);
    }
    let Err(OpenError::Version(e)) = open_for_writing(&path, &keys, &clock) else {
        panic!("expected invalid revision")
    };
    assert_eq!(e.key, Some("min_writer_revision"));
    let Err(failure) = open_for_writing_diagnostic(&path, &keys, &clock) else {
        panic!("expected malformed minimum")
    };
    assert!(matches!(*failure.error, OpenError::Version(_)));
    assert_eq!(failure.version.unwrap().exact_message, bad);
    revision(&conn, "min_writer_revision", "35");
    let required = {
        let file = StoreFile::open(&path).unwrap();
        let v = file.check_opened(&file.meta().unwrap(), "2.4.1").unwrap();
        assert_eq!(field(file.connection(), "PRAGMA query_only"), 1);
        let text = v.read_only_reason.unwrap();
        assert!(text.starts_with(&path_points));
        text
    };
    let Err(failure) = open_for_writing_diagnostic(&path, &keys, &clock) else {
        panic!("expected reader-only verdict")
    };
    assert!(matches!(
        *failure.error,
        OpenError::NotPorted(NotPorted::LaterWriter(_))
    ));
    assert_eq!(failure.read_only_reason, Some(required));
    revision(&conn, "min_writer_revision", "6");
    revision(&conn, "min_reader_revision", "35");
    let Err(OpenError::Version(e)) = open_for_writing(&path, &keys, &clock) else {
        panic!("expected reader minimum")
    };
    assert_eq!((e.key, e.needed), (Some("min_reader_revision"), Some(35)));
    let Err(failure) = open_for_writing_diagnostic(&path, &keys, &clock) else {
        panic!("expected reader minimum")
    };
    assert!(failure
        .version
        .unwrap()
        .exact_message
        .starts_with(&path_points));
    revision(&conn, "min_reader_revision", "1");
    let mut active = open_for_writing(&path, &keys, &clock).unwrap();
    revision(&conn, "min_writer_revision", "bad");
    let payload = PyValue::Dict(vec![(PyValue::str("name"), PyValue::str("temporary"))]);
    let (outcome, version) = active
        .with_version_diagnostic(|s| s.mutate("quarantine_delete", &payload, None, &mut clock));
    assert!(matches!(outcome, Err(WriteError::Version(_))));
    assert_eq!(version.unwrap().exact_message, bad);
    for effort in [true, false] {
        let ((result, logs), version) =
            active.with_version_diagnostic(|s| event(s, &mut clock, effort));
        assert!(matches!(
            result,
            Err(EventError::Write(WriteError::Version(_)))
        ));
        assert_eq!(version.unwrap().exact_message, bad);
        assert!(logs.is_empty());
        assert!(active.connection().is_autocommit());
        assert_eq!(field(active.connection(), "PRAGMA query_only"), 0);
    }
    assert_eq!(
        field(active.connection(), "SELECT count(*) FROM log"),
        initial
    );
    revision(&conn, "min_writer_revision", "35");
    let ((res, logs), fresh) = active.with_version_diagnostic(|s| event(s, &mut clock, false));
    assert!(fresh.is_none());
    let Err(EventError::Write(e @ WriteError::LaterWriter { .. })) = res else {
        panic!("newer writer")
    };
    assert!(logs.is_empty());
    let mut quoted = cps("attempt to write a readonly database: ");
    quoted.extend(bettermemory_core::printable::quoted(&path_points));
    assert!(e.message_points().starts_with(&quoted));
    assert!(bettermemory_core::store::record_event::sqlite_code(&e).is_none());
    assert_eq!(field(active.connection(), "PRAGMA query_only"), 1);
    active.close().unwrap();
    revision(&conn, "min_writer_revision", "6");
    let mut best = open_for_writing(&path, &keys, &clock).unwrap();
    revision(&conn, "min_writer_revision", "35");
    let (res, logs) = event(&mut best, &mut clock, true);
    assert!(res.unwrap().is_none());
    assert_eq!(
        logs,
        [bettermemory_core::store::record_event::Logged::NotRecorded(
            e.message_points()
        )]
    );
    best.close().unwrap();
    revision(&conn, "min_writer_revision", "6");
    let store = open_for_writing_with(&path, &keys, &clock, &mut |point| {
        if point == Boundary::Bound {
            conn.execute("DELETE FROM digest_state", []).unwrap();
            revision(&conn, "min_writer_revision", "bad");
        }
    })
    .unwrap();
    assert!(matches!(store.digest(), DigestOutcome::Left(_)));
    assert_eq!(store.digest_reason(), Some(bad.as_slice()));
    assert!(store.connection().is_autocommit());
    assert_eq!(
        field(store.connection(), "SELECT count(*) FROM log"),
        initial
    );
    store.close().unwrap();
    let locked = Connection::open(&path).unwrap();
    locked.execute_batch("BEGIN IMMEDIATE").unwrap();
    conn.execute_batch("PRAGMA busy_timeout=273").unwrap();
    let Err(DiagnosticError {
        error: UpgradeError::Version(_),
        version: Some(e),
    }) = prove_exclusive_at(&conn, &path)
    else {
        panic!("expected competing connection")
    };
    let cause = e.cause.as_deref().unwrap();
    assert_eq!(
        (cause.class, cause.code, cause.name()),
        ("OperationalError", Some(5), Some("SQLITE_BUSY"))
    );
    assert!(e
        .exact_message
        .starts_with(&bettermemory_core::printable::quoted(&path_points)));
    assert_eq!((e.key, e.needed), (None, None));
    assert_eq!(field(&conn, "PRAGMA busy_timeout"), 273);
    let mode: String = conn
        .query_row("PRAGMA locking_mode", [], |r| r.get(0))
        .unwrap();
    assert_eq!(mode, "normal");
    assert!(conn.is_autocommit());
    assert!(!locked.is_autocommit());
    locked.execute_batch("ROLLBACK").unwrap();
}
#[test]
fn normal_signed_store_and_literal_replacement_character() {
    exercise(OsStr::new("data-\u{fffd}"));
    exercise(OsStr::new("data-漢-🧪"));
}
#[cfg(target_os = "linux")]
#[test]
fn two_undecodable_filenames_are_distinct_from_each_other_and_a_literal_replacement() {
    use std::os::unix::ffi::OsStrExt;
    for raw in [b"data-\xff".as_slice(), b"data-\xfe".as_slice()] {
        exercise(OsStr::from_bytes(raw));
    }
}
#[cfg(windows)]
#[test]
fn diagnostic_os_text_preserves_unpaired_windows_units() {
    use std::{ffi::OsString, os::windows::ffi::OsStringExt};
    let mut u: Vec<_> = "root/./part/".encode_utf16().collect();
    u.push(0xdfff);
    let path = OsString::from_wide(&u);
    let mut expected = cps(r"root\part\");
    expected.push(0xdfff);
    assert_eq!(
        bettermemory_core::store::versions::path_text(Path::new(&path)),
        expected
    );
}
#[cfg(unix)]
#[test]
fn double_leading_slashes_and_raw_path_components_keep_pathlib_spelling() {
    use std::os::unix::ffi::OsStrExt;
    let p = Path::new(OsStr::from_bytes(b"//root/./\xff///end/../"));
    let mut expected = cps("//root/");
    expected.push(0xdcff);
    expected.extend(cps("/end/.."));
    assert_eq!(bettermemory_core::store::versions::path_text(p), expected);
}
