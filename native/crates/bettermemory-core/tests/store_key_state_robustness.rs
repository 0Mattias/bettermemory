//! Public read failures, physical spelling, callback ownership and concurrent one-shot warnings.
mod common;
use bettermemory_core::log::{ring::KeyFiles, Head};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::key_state::{
    head_status, warn_bound_elsewhere, warn_unclaimed, HeadStatus, KeyState, KeyStatus, Level,
    StateError, WarningError,
};
use bettermemory_core::unicode::cps;
use common::stores::scratch;
use rusqlite::Connection;
use std::path::Path;
use std::sync::{Arc, Barrier};

fn ring(dir: &Path) -> KeyFiles {
    KeyFiles::new(dir, &PyValue::str(&"a".repeat(32))).unwrap()
}

#[test]
fn concurrent_callers_share_one_warning_and_emitting_can_reenter() {
    let dir = scratch("key-state-threads");
    let state = KeyState::new(
        KeyStatus::Unclaimed,
        ring(&dir.0),
        dir.0.join("store"),
        cps("expected"),
    );
    let start = Arc::new(Barrier::new(8));
    let levels = std::thread::scope(|scope| {
        let handles: Vec<_> = (0..8)
            .map(|_| {
                let start = Arc::clone(&start);
                let s = &state;
                scope.spawn(move || {
                    start.wait();
                    let mut level = None;
                    warn_unclaimed(s, |w| {
                        level = Some(w.level);
                        warn_unclaimed(s, |nested| {
                            assert_eq!(nested.level, Level::Debug);
                            Ok::<_, ()>(())
                        })
                        .unwrap();
                        Ok::<_, ()>(())
                    })
                    .unwrap();
                    level.unwrap()
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|t| t.join().unwrap())
            .collect::<Vec<_>>()
    });
    assert_eq!(levels.iter().filter(|&&l| l == Level::Warning).count(), 1);
    assert_eq!(levels.iter().filter(|&&l| l == Level::Debug).count(), 7);
}
#[test]
fn guard_is_marked_before_a_fallible_or_panicking_callback() {
    let dir = scratch("key-state-callback");
    let r = ring(&dir.0);
    let first = dir.0.join("one");
    let second = dir.0.join("two");
    let error = Arc::new(String::from("original"));
    let failed = warn_bound_elsewhere(&r, &first, &cps(r.store_id()), &second, None, |w| {
        assert_eq!(w.level, Level::Warning);
        Err(Arc::clone(&error))
    });
    let Err(WarningError::Logger(returned)) = failed else {
        panic!("lost the callback failure")
    };
    assert!(Arc::ptr_eq(&returned, &error));
    let _ = std::panic::catch_unwind(|| {
        let _ = warn_bound_elsewhere(
            &r,
            &second,
            &cps(r.store_id()),
            &first,
            None,
            |w| -> Result<(), ()> {
                assert_eq!(w.level, Level::Warning);
                panic!("logger")
            },
        );
    })
    .unwrap_err();
    for (a, b) in [(&first, &second), (&second, &first)] {
        warn_bound_elsewhere(&r, a, &cps(r.store_id()), b, None, |w| {
            assert_eq!(w.level, Level::Debug);
            Ok::<_, ()>(())
        })
        .unwrap();
    }
}
#[cfg(unix)]
#[test]
fn both_bound_paths_resolve_but_unclaimed_keeps_binding_spelling() {
    use std::os::unix::fs::symlink;
    let d = scratch("key-state-alias");
    let root = &d.0;
    let real = root.join("real");
    std::fs::create_dir(&real).unwrap();
    let alias = root.join("alias");
    symlink(&real, &alias).unwrap();
    let files = ring(&real);
    let other = ring(&alias);
    let db = real.join("store");
    let aliasdb = alias.join("store");
    std::fs::write(&db, []).unwrap();
    let mut levels = Vec::new();
    for (p, q) in [(&db, real.join("other")), (&aliasdb, alias.join("other"))] {
        warn_bound_elsewhere(&files, p, &cps(files.store_id()), &q, None, |w| {
            levels.push(w.level);
            Ok::<_, ()>(())
        })
        .unwrap();
    }
    for (f, p) in [
        (files.clone(), db),
        (files, aliasdb.clone()),
        (other, aliasdb),
    ] {
        let s = KeyState::new(KeyStatus::Unclaimed, f, p, Vec::new());
        warn_unclaimed(&s, |w| {
            levels.push(w.level);
            Ok::<_, ()>(())
        })
        .unwrap();
    }
    assert_eq!(
        levels,
        [
            Level::Warning,
            Level::Debug,
            Level::Warning,
            Level::Debug,
            Level::Warning
        ]
    );
}
#[cfg(unix)]
#[test]
fn invalid_path_fails_before_logging() {
    let d = scratch("key-state-path-error");
    let f = ring(&d.0);
    let result = warn_bound_elsewhere(
        &f,
        Path::new("no\0name"),
        &cps(f.store_id()),
        &d.0,
        None,
        |_| -> Result<(), ()> { panic!("logger should not have run") },
    );
    let Err(WarningError::State(StateError::Raised { class, message })) = result else {
        panic!("missing path error")
    };
    assert_eq!(class, "ValueError");
    assert_eq!(message, cps("lstat: embedded null character in path"));
}
#[test]
fn a_typed_checkpoint_preserves_comparison_and_integer_conversion_order() {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch("CREATE TABLE log(seq INTEGER PRIMARY KEY, ts, session, kind, payload, prev_mac, mac);
    INSERT INTO log VALUES(1,'now',NULL,'search','{}','','first'),(2,'later',NULL,'search','{}','first','last');").unwrap();
    for (seq, mac, result) in [
        (PyValue::Float(1.0), "first", HeadStatus::Stale),
        (PyValue::Bool(true), "first", HeadStatus::Stale),
        (PyValue::Float(1.5), "first", HeadStatus::Mismatch),
        (PyValue::Float(f64::INFINITY), "x", HeadStatus::Past),
        (PyValue::Float(2.0), "last", HeadStatus::Ok),
    ] {
        assert_eq!(
            head_status(Some(&Head { seq, mac: cps(mac) }), &conn).unwrap(),
            result
        );
    }
    for (seq, class, text) in [
        (
            PyValue::Float(f64::NAN),
            "ValueError",
            "cannot convert float NaN to integer",
        ),
        (
            PyValue::Float(f64::NEG_INFINITY),
            "OverflowError",
            "cannot convert float infinity to integer",
        ),
        (
            PyValue::str("1"),
            "TypeError",
            "'>' not supported between instances of 'str' and 'int'",
        ),
    ] {
        let err = head_status(Some(&Head { seq, mac: cps("a") }), &conn).unwrap_err();
        let StateError::Raised {
            class: actual,
            message,
        } = err
        else {
            panic!("wrong error")
        };
        assert_eq!((actual, message), (class, cps(text)));
    }
}

#[cfg(unix)]
#[test]
fn absolute_warnings_resolve_in_a_removed_working_directory() {
    use std::os::unix::fs::symlink;
    use std::process::Command;

    const MARKER: &str = "BM_TEST_KEY_STATE_DELETED_CWD";
    if let Some(root) = std::env::var_os(MARKER) {
        let root = Path::new(&root);
        let actual = root.join("real");
        let alias = root.join("alias");
        let ring = ring(&actual);
        let vanished = root.join("vanished");
        std::env::set_current_dir(&vanished).unwrap();
        std::fs::remove_dir(&vanished).unwrap();
        assert!(std::env::current_dir().is_err());
        let mut levels = Vec::new();
        for place in [&actual, &alias] {
            warn_bound_elsewhere(
                &ring,
                &place.join("store"),
                &cps(ring.store_id()),
                &place.join("other"),
                None,
                |w| {
                    levels.push(w.level);
                    Ok::<_, ()>(())
                },
            )
            .unwrap();
        }
        for place in [&actual, &alias] {
            let s = KeyState::new(
                KeyStatus::Unclaimed,
                ring.clone(),
                place.join("second"),
                Vec::new(),
            );
            warn_unclaimed(&s, |w| {
                levels.push(w.level);
                Ok::<_, ()>(())
            })
            .unwrap();
        }
        assert_eq!(
            levels,
            [Level::Warning, Level::Debug, Level::Warning, Level::Debug]
        );
        let nul = actual.join("bad\0name");
        let err = warn_bound_elsewhere(
            &ring,
            &nul,
            &cps(ring.store_id()),
            &actual,
            None,
            |_| -> Result<(), ()> { panic!("unexpected sink") },
        );
        assert!(matches!(
            err,
            Err(WarningError::State(StateError::Raised {
                class: "ValueError",
                ..
            }))
        ));
        return;
    }
    let d = scratch("key-state-removed-cwd");
    let actual = d.0.join("real");
    std::fs::create_dir(&actual).unwrap();
    symlink(&actual, d.0.join("alias")).unwrap();
    std::fs::create_dir(d.0.join("vanished")).unwrap();
    let child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "absolute_warnings_resolve_in_a_removed_working_directory",
        ])
        .env(MARKER, &d.0)
        .output()
        .unwrap();
    assert!(
        child.status.success(),
        "{}",
        String::from_utf8_lossy(&child.stdout)
    );
}
