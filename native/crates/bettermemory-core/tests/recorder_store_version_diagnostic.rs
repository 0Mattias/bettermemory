//! The Recorder keeps the identity and exact fields of the same locked write failure.
mod common;
use bettermemory_core::{
    event_hook::HookScope,
    event_recorder::Recorder,
    fsutil::fsdecode,
    identity::Published,
    store::{
        create::{create, System},
        open::open_for_writing,
        record_event::EventTransaction,
        recorder::{secret_spans, StoreEventSink},
        VersionDiagnostic,
    },
    unicode::cps,
};
use std::{ffi::OsStr, sync::Arc};

fn exercise(name: &OsStr) {
    let temp = common::stores::scratch("recorder-version");
    let path = temp.0.join(name).join("memory.sqlite");
    let keys = temp.0.join("keys");
    let mut clock = System {
        engine_version: "2.4.1".into(),
    };
    create(&path, &keys, &mut clock).unwrap();
    let mut store = open_for_writing(&path, &keys, &clock).unwrap();
    store
        .connection()
        .execute(
            "UPDATE meta SET value='bad' WHERE key='min_writer_revision'",
            [],
        )
        .unwrap();
    let mut expected = fsdecode(path.as_os_str());
    expected.extend(cps(" carries a min_writer_revision that is not a whole number ('bad'); the store is not opened: set meta's min_writer_revision back to a whole number, or restore the store from a backup taken before the edit"));
    let mut warnings = Vec::new();
    for (kind, state) in [("search", true), ("diagnostic", false)] {
        let mut warning_text = cps(if state {
            "state row search not recorded, the call fails: StoreVersionError: "
        } else {
            "event diagnostic not recorded: "
        });
        warning_text.extend_from_slice(&expected);
        let mut logged = None;
        let mut recorder = Recorder {
            store: StoreEventSink {
                target: Ok(EventTransaction::Store(&mut store, &mut clock)),
                log: &mut warnings,
                spans: &mut secret_spans,
            },
            session_id: cps("disposable"),
            enabled: true,
            worktree_root: None,
            attribution: vec![],
            triggered_from: vec![],
            secret_spans: Box::new(secret_spans),
            declaration: Box::new(|_| Ok(None)),
            capture_worktree: Box::new(|| unreachable!()),
            warn: Box::new(|_| panic!("expected typed warning")),
        };
        let result = recorder.record_typed(
            &HookScope::new(),
            &Published::default(),
            &cps(kind),
            vec![],
            &mut |warning| {
                assert_eq!(warning.state, state);
                assert_eq!(warning.message(), warning_text);
                assert_eq!(warning.error.exception.class, "StoreVersionError");
                assert_eq!(warning.error.exception.message, expected);
                let version = warning.error.handle::<VersionDiagnostic>().unwrap();
                assert_eq!(version.exact_message, expected);
                assert_eq!(
                    (version.key, version.needed),
                    (Some("min_writer_revision"), None)
                );
                assert!(version.cause.is_none());
                assert!(warning.error.sqlite_code.is_none());
                assert!(logged.replace(warning.error.clone()).is_none());
                Ok(())
            },
        );
        let seen = logged.unwrap();
        if state {
            let raised = result.unwrap_err();
            assert!(raised.is_same(&seen));
            assert!(Arc::ptr_eq(
                &raised.handle::<VersionDiagnostic>().unwrap(),
                &seen.handle::<VersionDiagnostic>().unwrap()
            ));
        } else {
            result.unwrap();
        }
        drop(recorder);
        assert!(warnings.is_empty());
        assert!(store.connection().is_autocommit());
        let n: i64 = store
            .connection()
            .query_row("SELECT count(*) FROM log", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1);
    }
}

#[test]
fn state_and_measurement_keep_the_same_source_failure_on_a_unicode_store() {
    exercise(OsStr::new("raw-\u{fffd}"));
}

#[cfg(target_os = "linux")]
#[test]
fn two_distinct_raw_store_names_remain_distinct_through_the_recorder() {
    use std::os::unix::ffi::OsStrExt;
    for raw in [b"raw-\xff".as_slice(), b"raw-\xfe".as_slice()] {
        exercise(OsStr::from_bytes(raw));
    }
}
