//! Cases a plausible wrong `tool_handlers` fails: the bundle's
//! per-request resolution, its rebinding to a worker's store, the offload
//! path of `run`, and the refusal texts' odd corners the reference keeps.

mod common;

use std::sync::{Arc, Mutex};

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::tool_handlers::{
    in_the_way, kept_or_retryable, read_only, refusals_as_tool_errors, wrap_code, Failure, Offload,
    RefusalClass, SqliteFailure, StoreSource,
};
use bettermemory_core::unicode::cps;
use common::tool_handlers::{bundle, retryable, Bundle, Fixed, TestRecorder, TestStore};

fn busy(text: &str) -> Failure {
    Failure::Sqlite(SqliteFailure {
        code: Some(5),
        text: cps(text),
    })
}

#[test]
fn for_request_is_self_when_the_source_resolves_the_same_store() {
    let deps = bundle(TestStore::new(1), None, None);
    let got = deps.for_request(None);
    assert!(matches!(got, std::borrow::Cow::Borrowed(_)));
}

#[test]
fn for_request_copies_with_no_offload_and_keeps_the_recorder() {
    let other = TestStore::new(2);
    let source: Arc<dyn StoreSource<TestStore>> = Arc::new(Fixed(other.clone()));
    let offload: Offload<TestStore> = Arc::new(|f| f(TestStore::new(9)));
    let deps = bundle(TestStore::new(1), Some(source), Some(offload));
    let got = deps.for_request(None).into_owned();
    assert_eq!(got.store.id, 2);
    assert!(got.offload.is_none());
    // A shallow copy: the recorder still writes through the bundle's store.
    assert_eq!(got.recorder, TestRecorder(1));
    assert!(deps.offload.is_some());
}

#[test]
fn a_store_equal_in_value_but_another_object_is_not_the_same() {
    let mine = TestStore::new(1);
    let twin = TestStore {
        id: 1,
        commits: Arc::new(std::sync::atomic::AtomicU64::new(7)),
    };
    let source: Arc<dyn StoreSource<TestStore>> = Arc::new(Fixed(twin));
    let deps = bundle(mine, Some(source), None);
    assert!(matches!(deps.for_request(None), std::borrow::Cow::Owned(_)));
}

#[test]
fn with_store_rebinds_the_store_and_the_recorder() {
    let deps = bundle(TestStore::new(1), None, None);
    let got = deps.with_store(TestStore::new(3));
    assert_eq!(got.store.id, 3);
    assert_eq!(got.recorder, TestRecorder(3));
    assert_eq!(deps.recorder, TestRecorder(1));
}

#[test]
fn run_offloads_to_the_workers_store_and_counts_its_commits() {
    let worker = TestStore::new(5);
    let w = worker.clone();
    let seen = Arc::new(Mutex::new(Vec::new()));
    let s2 = Arc::clone(&seen);
    let offload: Offload<TestStore> = Arc::new(move |f| {
        s2.lock().unwrap().push("worker");
        f(w.clone())
    });
    let deps = bundle(TestStore::new(1), None, Some(offload));
    let body = |d: &Bundle| {
        assert_eq!(d.store.id, 5);
        assert_eq!(d.recorder, TestRecorder(5));
        d.store.commit();
        Err(busy("locked"))
    };
    let got = deps.run(&body, &retryable).unwrap_err();
    let Failure::Tool(r) = got else { panic!() };
    assert_eq!(r.class, RefusalClass::KeptWrite);
    assert_eq!(*seen.lock().unwrap(), vec!["worker"]);
    // The bundle's own store saw nothing.
    assert_eq!(
        deps.store.commits.load(std::sync::atomic::Ordering::SeqCst),
        7
    );
}

#[test]
fn run_inline_is_retryable_when_nothing_was_kept() {
    let deps = bundle(TestStore::new(1), None, None);
    let body = |_: &Bundle| Err(busy("locked"));
    let Failure::Tool(r) = deps.run(&body, &retryable).unwrap_err() else {
        panic!()
    };
    assert_eq!(r.class, RefusalClass::Retryable);
    assert_eq!(r.code, Some("store_busy"));
}

#[test]
fn a_read_only_error_is_not_retried_in_the_body() {
    let deps = bundle(TestStore::new(1), None, None);
    let ro = SqliteFailure {
        code: Some(8),
        text: cps("ro"),
    };
    let body = |_: &Bundle| Err(Failure::Sqlite(ro.clone()));
    assert_eq!(
        kept_or_retryable(&body, &deps, &retryable),
        Err(Failure::Sqlite(ro.clone()))
    );
}

#[test]
fn read_only_reads_the_low_byte_of_negative_codes() {
    let f = |c| {
        read_only(&SqliteFailure {
            code: Some(c),
            text: vec![],
        })
    };
    assert!(f(8));
    assert!(f(8 | 3 << 8));
    assert!(f(-248));
    assert!(!f(-8));
    assert!(!f(0x88));
    assert!(f(wrap_code("-1180591620717411303416").unwrap()));
    assert_eq!(wrap_code("18446744073709551621"), Some(5));
    assert_eq!(wrap_code("-1"), Some(-1));
    assert_eq!(wrap_code("5x"), None);
    assert_eq!(wrap_code("-"), None);
    assert!(!f(0));
    assert!(!read_only(&SqliteFailure {
        code: None,
        text: vec![]
    }));
}

#[test]
fn an_unknown_code_reads_as_busy() {
    assert_eq!(in_the_way("store_busy"), in_the_way("anything"));
    assert_ne!(in_the_way("store_full"), in_the_way("store_busy"));
}

#[test]
fn store_refusal_text_keeps_control_tokens_a_value_error_loses() {
    let tok = "<|im_start|>";
    let got = refusals_as_tool_errors(|| Err(busy(tok)), &retryable).unwrap_err();
    let Failure::Tool(r) = got else { panic!() };
    let text: String = r.text.iter().map(|&c| char::from_u32(c).unwrap()).collect();
    assert!(text.contains(tok));
    let got = refusals_as_tool_errors(|| Err(Failure::Value(cps(tok))), &retryable).unwrap_err();
    let Failure::Tool(r) = got else { panic!() };
    assert_eq!(r.text, cps("im_start"));
    assert_eq!(r.class, RefusalClass::Refused);
}

#[test]
fn a_success_is_named_tokens_of_the_value() {
    let got = refusals_as_tool_errors(|| Ok(PyValue::str("[INST]")), &retryable).unwrap();
    assert_eq!(got, PyValue::str("INST"));
}
