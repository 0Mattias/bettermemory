//! Replay the actual Python recorder on a signed store and compare both
//! connection views, all seven row columns, the head and ordered diagnostics.
mod common;

#[cfg(not(unix))]
#[test]
fn signed_store_fixture_requires_posix_key_binding() {
    for file in [
        "recorder_store_posix.jsonl",
        "recorder_store_python_posix.jsonl",
    ] {
        common::parity::record(
            "golden_recorder_store",
            file,
            "os",
            0,
            &["signed recorder groups".into()],
            &["no POSIX key binding on this host".into()],
        );
    }
}

#[cfg(unix)]
mod posix {
    use std::{cell::RefCell, collections::VecDeque, io, path::Path, rc::Rc};

    use super::common::{self, opened, written};
    use bettermemory_core::{
        datetime::DateTime,
        event_hook::{BusyTimeout, HookScope, Raised},
        event_recorder::{ActorKeyIdentity, PyException, Recorder, RecorderFields},
        identity::{Actor, Caller, Published},
        log::Key,
        store::{
            create::Provider,
            keys::load_key_ring,
            open::open_for_writing,
            record_event::{EventLogger, EventTransaction, Logged},
            recorder::{secret_spans, StoreEventSink},
        },
        unicode::cps,
    };
    use rusqlite::Connection;
    use serde_json::{json, Value};

    #[derive(Default)]
    struct Trace {
        clock: VecDeque<DateTime>,
        mono: Vec<i64>,
        pauses: Vec<f64>,
        log: Vec<Value>,
        lock: Option<Connection>,
        release: bool,
    }

    struct Clock(Rc<RefCell<Trace>>);
    impl Provider for Clock {
        fn store_id(&mut self) -> io::Result<String> {
            unreachable!()
        }
        fn key(&mut self) -> io::Result<Key> {
            unreachable!()
        }
        fn membership_store_id(&mut self) -> io::Result<String> {
            unreachable!()
        }
        fn engine_version(&self) -> String {
            opened::ENGINE.into()
        }
        fn now(&mut self) -> io::Result<DateTime> {
            Ok(self
                .0
                .borrow_mut()
                .clock
                .pop_front()
                .expect("unscripted log clock"))
        }
        fn monotonic(&mut self) -> io::Result<f64> {
            self.0.borrow_mut().mono.push(3);
            Ok(3.0)
        }
        fn pause(&mut self, seconds: f64) {
            let mut trace = self.0.borrow_mut();
            trace.pauses.push(seconds);
            if trace.release {
                trace
                    .lock
                    .as_ref()
                    .unwrap()
                    .execute_batch("COMMIT")
                    .unwrap();
            }
        }
    }

    struct Logger(Rc<RefCell<Trace>>);
    impl EventLogger for Logger {
        fn emit(&mut self, kind: &[u32], entry: Logged) -> Result<(), PyException> {
            let mut message = cps("event ");
            message.extend(kind);
            let (level, reason, retry) = match entry {
                Logged::Retry {
                    reason,
                    next,
                    tries,
                } => ("INFO", reason, Some((next.to_string(), tries.to_string()))),
                Logged::WideRetry {
                    reason,
                    next,
                    tries,
                } => ("INFO", reason, Some((next, tries))),
                Logged::NotRecorded(reason) => ("WARNING", reason, None),
                Logged::HeadCheckpoint { .. } => panic!("unexpected checkpoint failure"),
            };
            if let Some((next, tries)) = retry {
                message.extend(cps(": the store was busy ("));
                message.extend(reason);
                message.extend(cps(&format!("); try {next} of {tries}")));
            } else {
                message.extend(cps(" not recorded: "));
                message.extend(reason);
            }
            self.0
                .borrow_mut()
                .log
                .push(json!(["bettermemory.store", level, message]));
            Ok(())
        }
    }

    #[derive(Debug)]
    struct Never;
    impl Raised for Never {
        fn class_name(&self) -> &str {
            unreachable!()
        }
        fn sqlite_code(&self) -> Option<i64> {
            unreachable!()
        }
    }

    fn steps(
        mut target: Result<EventTransaction<'_, '_>, &[u32]>,
        trace: &Rc<RefCell<Trace>>,
        case: &Value,
        file: &Path,
    ) {
        let read = Connection::open(file).unwrap();
        let mut logger = Logger(trace.clone());
        let target = target
            .as_mut()
            .map(EventTransaction::reborrow)
            .map_err(|text| *text);
        let sink = StoreEventSink {
            target,
            log: &mut logger,
            spans: &mut secret_spans,
        };
        let mut recorder = Recorder {
            store: sink,
            session_id: cps("process"),
            enabled: true,
            worktree_root: Some(cps("/fixture")),
            attribution: cps("cli_fixture"),
            triggered_from: cps("hook"),
            secret_spans: Box::new(secret_spans),
            declaration: Box::new(|_| Ok(None)),
            capture_worktree: Box::new(|| unreachable!()),
            warn: Box::new(|_| panic!("rich logger bypassed")),
        };
        for (index, step) in case["steps"].as_array().unwrap().iter().enumerate() {
            let spec = &step["call"];
            let mut state = trace.borrow_mut();
            state.clock = step["clock"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| written::instant(v.as_str().unwrap()))
                .collect();
            state.log.clear();
            state.mono.clear();
            state.pauses.clear();
            state.release = spec["release"] == true;
            if spec["lock"] == true {
                let lock = Connection::open(file).unwrap();
                lock.execute_batch("BEGIN IMMEDIATE").unwrap();
                state.lock = Some(lock);
            }
            drop(state);
            recorder.enabled = spec["enabled"].as_bool().unwrap_or(true);
            let mut published = Published::default();
            if let Some(id) = spec["caller"].as_str() {
                let mut actor = Actor {
                    session: Some(cps(id)),
                    ..Actor::default()
                };
                actor.set_source("session", "header");
                published.publish(Caller {
                    actor,
                    harness_session: Some(cps(&format!("h-{id}"))),
                    ..Caller::default()
                });
            }
            let fields: Vec<_> = spec["fields"]
                .as_array()
                .unwrap()
                .iter()
                .map(|f| (cps(f[0].as_str().unwrap()), common::neutral(&f[1])))
                .collect();
            let fields = if let Some(raw) = spec["raw_json"].as_str() {
                RecorderFields::from_json(&cps(raw)).unwrap()
            } else {
                let mut fields = RecorderFields::from(fields);
                let mut ids = std::collections::HashMap::<u64, ActorKeyIdentity>::new();
                for row in spec["aliases"].as_array().into_iter().flatten() {
                    let id = ids.entry(row[2].as_u64().unwrap()).or_default();
                    fields.actor_keys.bind(
                        row[0].as_u64().unwrap() as usize,
                        row[1].as_u64().unwrap() as usize,
                        id.clone(),
                    );
                }
                fields
            };
            if let Some(expected) = spec["canonical_sha"].as_str() {
                use bettermemory_core::{json::canonical_payload, store::recorder::actor_record};
                let (key, value) = &fields.fields[0];
                let raw = if *key == cps("actor") {
                    canonical_payload(&actor_record(value, &fields.actor_keys).unwrap().0).unwrap()
                } else {
                    canonical_payload(value).unwrap()
                };
                assert_eq!(
                    bettermemory_core::log::fingerprint(raw.as_bytes()),
                    expected,
                    "direct canonical JSON"
                );
            }
            if let Some(survivors) = spec["survivors"].as_array() {
                let (_, value) = fields
                    .fields
                    .iter()
                    .find(|(k, _)| *k == cps("actor"))
                    .unwrap();
                let (mapping, _) =
                    bettermemory_core::store::recorder::actor_record(value, &fields.actor_keys)
                        .unwrap();
                let bettermemory_core::pyvalue::PyValue::Dict(items) = &mapping else {
                    panic!("mapping")
                };
                assert_eq!(items.len(), survivors.len());
                for ((_, value), expected) in items.iter().zip(survivors) {
                    assert_eq!(
                        value,
                        &bettermemory_core::pyvalue::PyValue::str(expected.as_str().unwrap())
                    );
                }
            }
            let kind = cps(spec["kind"].as_str().unwrap());
            let run = |scope: &mut HookScope| {
                recorder.record_fields(scope, &published, &kind, fields, &mut |w| {
                    trace.borrow_mut().log.push(json!([
                        "bettermemory.events",
                        "WARNING",
                        w.message()
                    ]));
                    Ok(())
                })
            };
            let mut scope = HookScope::new();
            let out = if spec["suppressed"] == true {
                scope.suppressed(run)
            } else if spec["hook"] == true {
                scope
                    .within_hook_budget::<_, Never>("stop", None, &mut || 0.0, |s, _| Ok(run(s)))
                    .unwrap()
            } else {
                run(&mut scope)
            };
            let state = &mut *trace.borrow_mut();
            let raised = out
                .err()
                .map(|e| json!([e.exception.class, e.exception.message, e.sqlite_code]));
            let conn = match &recorder.store.target {
                Ok(EventTransaction::Store(s, _)) => s.connection(),
                Ok(EventTransaction::Joined(t)) => t.connection(),
                Err(_) => &read,
            };
            let check = |sql| conn.query_row(sql, [], |r| r.get::<_, i64>(0)).unwrap();
            let label = format!("{} step {index}", case["name"]);
            assert_eq!(json!(raised), step["raised"], "{label}: raised");
            assert!(state.clock.is_empty(), "{label}: unused clock");
            assert_eq!(json!(state.log), step["log"], "{label}: logs");
            assert_eq!(json!(state.mono), step["mono"], "{label}: monotonic");
            assert_eq!(json!(state.pauses), step["pauses"], "{label}: pauses");
            assert_eq!(
                json!(check("SELECT max(seq) FROM log")),
                step["inside"],
                "{label}: caller"
            );
            assert_eq!(
                written::log_rows(file, 1),
                step["outside"],
                "{label}: outside"
            );
            assert_eq!(
                json!(check("PRAGMA busy_timeout")),
                step["timeout"],
                "{label}: timeout"
            );
            state.lock = None;
        }
    }

    #[test]
    fn signed_recorder_matches_each_python_call() {
        for file in [
            "recorder_store_posix.jsonl",
            "recorder_store_python_posix.jsonl",
        ] {
            let mut lines = common::jsonl(file).into_iter();
            let count = lines.next().unwrap()["count"].as_u64().unwrap() as usize;
            let mut ledger = common::parity::Ledger::new("golden_recorder_store", file);
            for case in lines {
                ledger.case(case["name"].as_str().unwrap(), || run(&case));
            }
            ledger.finish(count);
        }
    }

    fn run(case: &Value) {
        let dir = common::stores::scratch("recorder-signed");
        let root = dir.0.join("case");
        opened::made("created", &root);
        let file = root.join("memory.sqlite");
        let keys = root.join("keys");
        let mut store = open_for_writing(&file, &keys, &opened::engine_provider()).unwrap();
        let id = store.store_id().to_owned();
        let trace = Rc::new(RefCell::new(Trace::default()));
        let mut clock = Clock(trace.clone());
        let name = case["name"].as_str().unwrap();
        if name == "locked" {
            store.set_busy_timeout(0.0).unwrap();
        }
        if name == "readonly" {
            store
                .connection()
                .execute_batch("PRAGMA query_only=ON")
                .unwrap();
        }
        if name.starts_with("joined") {
            let mut tx = store.begin(&mut clock).unwrap();
            steps(Ok(EventTransaction::Joined(&mut tx)), &trace, case, &file);
            if name == "joined_commit" {
                trace
                    .borrow_mut()
                    .clock
                    .push_back(written::instant("2025-01-01T00:00:00Z"));
                tx.commit().unwrap();
            }
        } else if name == "keyless" {
            let refusal: Vec<u32> =
                serde_json::from_value(case["keyless_refusal"].clone()).unwrap();
            steps(Err(&refusal), &trace, case, &file);
        } else {
            steps(
                Ok(EventTransaction::Store(&mut store, &mut clock)),
                &trace,
                case,
                &file,
            );
        }
        assert_eq!(
            written::log_rows(&file, 1),
            case["rows"],
            "{name} commit/rollback"
        );
        let ring = load_key_ring(
            &root.join(if name == "keyless" { "absent" } else { "keys" }),
            &id,
        )
        .unwrap();
        let got = ring.read_head().unwrap().map(|h| {
            let bettermemory_core::pyvalue::PyValue::Int(s) = &h.seq else {
                panic!("head seq")
            };
            json!([
                s.parse::<i64>().unwrap(),
                bettermemory_core::unicode::to_string(&h.mac).unwrap()
            ])
        });
        assert_eq!(json!(got), case["head"], "{name} head");
    }
}
