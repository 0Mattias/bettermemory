//! Hostile input to `store::record_event`'s pure leaves: very long and
//! deeply nested payload texts, non-finite and extreme deadlines, every
//! SQLite code. Nothing panics, in debug or release, and naming a long
//! text grows no faster than its length.

mod timing;

use bettermemory_core::store::record_event::{busy, named_payload, time_left};
use bettermemory_core::store::write::WriteError;
use rusqlite::ffi;

#[test]
fn every_code_and_deadline_answers() {
    for code in [i32::MIN, -1, 0, 5, 6, 255, 256, 261, 517, i32::MAX] {
        let _ = busy(&WriteError::Sqlite(rusqlite::Error::SqliteFailure(
            ffi::Error::new(code),
            None,
        )));
    }
    for d in [
        f64::NAN,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::MAX,
        f64::MIN,
        0.0,
        -0.0,
    ] {
        for now in [f64::NAN, f64::INFINITY, 0.0, f64::MAX] {
            let _ = time_left(Some(d), now);
        }
    }
    assert!(!time_left(Some(f64::NAN), 0.0));
}

#[test]
fn deep_and_long_payloads_do_not_panic() {
    let deep = "[".repeat(100_000) + &"]".repeat(100_000);
    assert_eq!(named_payload(&deep).expect("kept"), deep);
    let deep_obj = format!("{{\"note\":{}{}}}", "[".repeat(5_000), "]".repeat(5_000));
    let _ = named_payload(&deep_obj);
    let long = format!("{{\"note\":\"{}\"}}", "<|im_start|>".repeat(20_000));
    assert!(named_payload(&long).expect("named").len() < long.len());
}

#[test]
fn naming_grows_linearly() {
    let text = |n: usize| format!("{{\"note\":\"{}\"}}", "a <|im_start|> ".repeat(n));
    let (small, large) = (text(2_000), text(20_000));
    timing::grows_within(
        "named_payload",
        30,
        &mut || timing::once(|| named_payload(&small)),
        &mut || timing::once(|| named_payload(&large)),
    );
}
