//! On Windows the reference's `Path.is_file` stats a transcript path
//! holding a lone surrogate through the wide string; the port's
//! `host_bytes` declines it (`Fault::NotPorted`). Here the gap is
//! recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn a_lone_surrogate_path_is_not_ported_here() {
    common::parity::record(
        "recall_settle_interrupted_kills",
        "recall_settle_interrupted.jsonl",
        "os",
        0,
        &["a transcript path holding a lone surrogate".to_owned()],
        &["the wide-string stat of such a path is not ported".to_owned()],
    );
}
