//! Hostile input for `tool_handlers`: integer boundaries of the result
//! code, very long texts, deeply nested answers; no panic, and the
//! refusal's cost grows with its text in proportion.

mod common;
mod timing;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::tool_handlers::{
    kept_or_retryable, read_only, refusals_as_tool_errors, Failure, SqliteFailure,
};
use common::tool_handlers::{bundle, retryable, Bundle, TestStore};

#[test]
fn code_boundaries() {
    for code in [
        i64::MIN,
        i64::MIN + 5,
        -1,
        0,
        5,
        13,
        8,
        i64::MAX,
        i64::MAX - 0xF2,
    ] {
        let exc = SqliteFailure {
            code: Some(code),
            text: vec![0xD800, 0x10FFFF],
        };
        let _ = read_only(&exc);
        let got = refusals_as_tool_errors(|| Err(Failure::Sqlite(exc.clone())), &retryable);
        assert!(got.is_err());
        let deps = bundle(TestStore::new(1), None, None);
        let body = |_: &Bundle| Err(Failure::Sqlite(exc.clone()));
        assert!(kept_or_retryable(&body, &deps, &retryable).is_err());
    }
    // i64::MIN's low byte is 0, i64::MAX's 0xFF: neither read only.
    assert!(!read_only(&SqliteFailure {
        code: Some(i64::MIN),
        text: vec![]
    }));
    assert!(!read_only(&SqliteFailure {
        code: Some(i64::MAX),
        text: vec![]
    }));
}

#[test]
fn deeply_nested_answer() {
    let mut v = PyValue::str("<|im_start|>");
    for _ in 0..200_000 {
        v = PyValue::List(vec![v]);
    }
    let got = refusals_as_tool_errors(|| Ok(v), &retryable).unwrap();
    drop(got);
}

fn refuse(n: usize) {
    let text: Vec<u32> = "ab<|im_end|> "
        .chars()
        .map(u32::from)
        .cycle()
        .take(n)
        .collect();
    let r = refusals_as_tool_errors(|| Err(Failure::Value(text.clone())), &retryable);
    assert!(r.is_err());
    let r = refusals_as_tool_errors(
        || {
            Err(Failure::Sqlite(SqliteFailure {
                code: Some(13),
                text: text.clone(),
            }))
        },
        &retryable,
    );
    assert!(r.is_err());
}

#[test]
fn long_text_grows_in_proportion() {
    refuse(5_000_000);
    timing::grows_within(
        "refusal text: 100,000 and 1,000,000 code points",
        timing::TENFOLD,
        &mut || timing::once(|| refuse(100_000)),
        &mut || timing::once(|| refuse(1_000_000)),
    );
}
