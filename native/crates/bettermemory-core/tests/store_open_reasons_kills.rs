//! The borrowed connection, compatibility projection, and original physical file.
mod common;

use bettermemory_core::store::{
    open_checks_at_points, open_checks_points, OpenCheckError, StoreError,
};
use bettermemory_core::unicode::cps;
use rusqlite::hooks::{AuthAction, AuthContext, Authorization};
use rusqlite::Connection;

#[test]
fn only_the_compatibility_api_projects_surrogates_to_scalar_text() {
    let path = [97, 0xdcff, 0xfffd];
    let db = Connection::open_in_memory().unwrap();
    let Err(OpenCheckError::ValueError(exact)) = open_checks_at_points(&db, &[], &path, "1") else {
        panic!("expected a metadata refusal")
    };
    assert_eq!(&exact[..path.len()], &path);
    let legacy = open_checks_points(&[], &path, "1").unwrap_err();
    assert!(legacy.version.is_none());
    let StoreError::Refused(message) = legacy.error else {
        panic!("expected ValueError")
    };
    assert_eq!(message, "a�� carries no store id or key fingerprint");
}

#[test]
fn sqlite_failure_follows_metadata_and_version_checks() {
    let mut conn = Connection::open_in_memory().unwrap();
    conn.execute_batch("BEGIN").unwrap();
    conn.authorizer(Some(|context: AuthContext<'_>| match context.action {
        AuthAction::Pragma {
            pragma_name: "query_only",
            pragma_value: Some(_),
        } => Authorization::Deny,
        _ => Authorization::Allow,
    }))
    .unwrap();
    let mut meta = vec![
        ("store_id".to_owned(), "b".repeat(32)),
        ("key_fingerprint".to_owned(), "f".to_owned()),
        ("min_writer_revision".to_owned(), "99".to_owned()),
    ];
    assert!(matches!(
        open_checks_at_points(&conn, &meta, &[], "1"),
        Err(OpenCheckError::Sqlite(_))
    ));
    meta.push(("min_reader_revision".into(), "99".into()));
    let Err(OpenCheckError::Version(error)) = open_checks_at_points(&conn, &meta, &[0xdcfe], "1")
    else {
        panic!("expected version refusal")
    };
    assert_eq!(
        (error.exact_message[0], error.key, error.needed),
        (0xdcfe, Some("min_reader_revision"), Some(99))
    );
    meta.push(("schema_version".into(), "wrong".into()));
    let Err(OpenCheckError::ValueError(error)) =
        open_checks_at_points(&conn, &meta, &[0xdcfe], "1")
    else {
        panic!("expected schema ValueError")
    };
    assert_eq!(
        error,
        cps("invalid literal for int() with base 10: 'wrong'")
    );
    assert!(!conn.is_autocommit());
    assert_eq!(
        conn.query_row("PRAGMA query_only", [], |r| r.get::<_, i32>(0))
            .unwrap(),
        0
    );
}

#[test]
#[cfg(unix)]
fn one_file_can_have_two_distinct_python_path_spellings() {
    use bettermemory_core::fsutil::fsdecode;
    use bettermemory_core::store::{
        create::{create, System},
        StoreFile,
    };
    let root = common::stores::scratch("open-reason");
    let path = root.0.join("café").join("memory.sqlite");
    create(
        &path,
        &root.0.join("keys"),
        &mut System {
            engine_version: "2.4.1".into(),
        },
    )
    .unwrap();
    {
        let writer = Connection::open(&path).unwrap();
        writer
            .execute(
                "UPDATE meta SET value = '9' WHERE key = 'schema_version'",
                [],
            )
            .unwrap();
    }
    let store = StoreFile::open(&path).unwrap();
    let meta = store.meta().unwrap();
    let scalar = fsdecode(path.as_os_str());
    let mut escaped = Vec::new();
    for &cp in &scalar {
        if cp == 0xe9 {
            escaped.extend([0xdcc3, 0xdca9]);
        } else {
            escaped.push(cp);
        }
    }
    assert_ne!(scalar, escaped);
    for spelling in [scalar, escaped] {
        let Err(OpenCheckError::ValueError(message)) =
            store.check_opened_as(&meta, &spelling, "2.4.1")
        else {
            panic!("expected later schema")
        };
        assert_eq!(
            message,
            [spelling, cps(" is schema 9; this build reads up to 1")].concat()
        );
    }
}
