//! The production recorder must apply Python inheritance to the actual
//! exception object, including on the far side of SQLite COMMIT.
mod common;

use bettermemory_core::{
    event_hook::HookScope,
    event_recorder::{EventSink, PyException, Recorder},
    identity::Published,
    store::{
        exception::{ExceptionFamily, StoreException},
        record_event::{EventLogger, Logged},
        recorder::{secret_spans, StoreEventSink},
        write::WriteError,
    },
    unicode::cps,
};
use std::sync::Arc;

#[derive(Debug)]
struct Handle(usize);

fn fault(family: ExceptionFamily, tag: usize) -> StoreException {
    StoreException::new(
        PyException {
            class: "SameFault".into(),
            message: vec![120, 0xdcff],
        },
        family,
    )
    .with_handle(Arc::new(Handle(tag)))
}

fn recorder<S: EventSink>(store: S) -> Recorder<S> {
    Recorder {
        store,
        session_id: cps("process"),
        enabled: true,
        worktree_root: None,
        attribution: vec![],
        triggered_from: vec![],
        secret_spans: Box::new(secret_spans),
        declaration: Box::new(|_| Ok(None)),
        capture_worktree: Box::new(|| unreachable!()),
        warn: Box::new(|_| panic!("expected a structured logger")),
    }
}

struct RaisingLogger(StoreException);
impl EventLogger for RaisingLogger {
    fn emit(&mut self, _: &[u32], _: Logged) -> Result<(), PyException> {
        unreachable!()
    }
    fn emit_typed(
        &mut self,
        _: &[u32],
        entry: Logged,
        cause: Option<&WriteError>,
    ) -> Result<(), StoreException> {
        assert!(matches!(entry, Logged::NotRecorded(_)));
        assert!(cause.is_none());
        Err(self.0.clone())
    }
}

fn check<S: EventSink>(
    rec: &mut Recorder<S>,
    error: &StoreException,
    state: bool,
    replacement: bool,
) {
    let mut calls = 0;
    let replacement_error = fault(ExceptionFamily::Base, 44);
    let out = rec.record_typed(
        &HookScope::new(),
        &Published::default(),
        &cps(if state { "write" } else { "recall" }),
        vec![],
        &mut |warn| {
            calls += 1;
            assert!(warn.error.is_same(error));
            assert_eq!(warn.state, state);
            assert!(Arc::ptr_eq(
                &warn.error.handle::<Handle>().unwrap(),
                &error.handle::<Handle>().unwrap()
            ));
            assert_eq!(warn.error.handle::<Handle>().unwrap().0, 9);
            if replacement {
                Err(replacement_error.clone())
            } else {
                Ok(())
            }
        },
    );
    let caught = error.family != ExceptionFamily::Base;
    assert_eq!(calls, usize::from(caught));
    if caught && replacement {
        assert!(out.unwrap_err().is_same(&replacement_error));
    } else if !caught || state {
        assert!(out.unwrap_err().is_same(error));
    } else {
        out.unwrap();
    }
}

#[test]
fn identical_names_and_messages_do_not_determine_recorder_catch_or_identity() {
    for family in [ExceptionFamily::Other, ExceptionFamily::Base] {
        for state in [false, true] {
            for replace in [false, true] {
                let error = fault(family, 9);
                assert!(!error.is_same(&fault(family, 9)));
                let mut logger = RaisingLogger(error.clone());
                let sink = StoreEventSink {
                    target: Err(&[]),
                    log: &mut logger,
                    spans: &mut secret_spans,
                };
                check(&mut recorder(sink), &error, state, replace);
            }
        }
    }
}

#[cfg(unix)]
#[test]
fn a_clock_can_raise_the_original_object_before_or_after_the_durable_row() {
    use bettermemory_core::{
        datetime::DateTime,
        log::Key,
        store::{create::Provider, open::open_for_writing, record_event::EventTransaction},
    };
    use common::{opened, written};
    use std::io;
    struct Clock {
        error: StoreException,
        at: usize,
        reads: usize,
    }
    impl Provider for Clock {
        fn store_id(&mut self) -> io::Result<String> {
            unreachable!()
        }
        fn membership_store_id(&mut self) -> io::Result<String> {
            unreachable!()
        }
        fn key(&mut self) -> io::Result<Key> {
            unreachable!()
        }
        fn engine_version(&self) -> String {
            opened::ENGINE.into()
        }
        fn now(&mut self) -> io::Result<DateTime> {
            unreachable!()
        }
        fn log_now(&mut self) -> Result<DateTime, WriteError> {
            self.reads += 1;
            if self.reads == self.at {
                return Err(WriteError::Callback(self.error.clone()));
            }
            Ok(written::instant("2024-06-01T00:00:00Z"))
        }
    }
    for at in [1, 2] {
        for state in [false, true] {
            for family in [ExceptionFamily::Other, ExceptionFamily::Base] {
                let dir = common::stores::scratch("recstore-errors");
                let root = dir.0.join("case");
                opened::made("created", &root);
                let file = root.join("memory.sqlite");
                let mut store =
                    open_for_writing(&file, &root.join("keys"), &opened::engine_provider())
                        .unwrap();
                let error = fault(family, 9);
                let mut clock = Clock {
                    error: error.clone(),
                    at,
                    reads: 0,
                };
                let mut logs = Vec::new();
                let sink = StoreEventSink {
                    target: Ok(EventTransaction::Store(&mut store, &mut clock)),
                    log: &mut logs,
                    spans: &mut secret_spans,
                };
                check(&mut recorder(sink), &error, state, false);
                assert_eq!(clock.reads, at);
                assert!(logs.is_empty());
                assert_eq!(written::last_seq(&file), at as i64);
                assert!(store.connection().is_autocommit());
                let head = bettermemory_core::store::keys::load_key_ring(
                    &root.join("keys"),
                    store.store_id(),
                )
                .unwrap()
                .read_head()
                .unwrap()
                .unwrap();
                assert_eq!(head.seq, bettermemory_core::pyvalue::PyValue::int(1));
            }
        }
    }
}
