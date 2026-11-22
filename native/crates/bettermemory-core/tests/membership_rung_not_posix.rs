//! `golden_membership_rung.rs` runs everywhere but compares one row less
//! off POSIX: the `_token_read` row whose token path is a directory.
//! Windows refuses to open a directory, and `StdFs` ports no `strerror`
//! text for Windows, so the read declines (`Fault::NotPorted`) where the
//! reference answers None (a PermissionError read as unreadable). The
//! gap is recorded here.

#![cfg(not(unix))]

mod common;

#[test]
fn the_token_read_of_a_directory_has_no_case_here() {
    common::parity::record(
        "golden_membership_rung",
        "membership_rung.jsonl",
        "os",
        0,
        &["the token_read row whose token path is a directory".to_owned()],
        &["no strerror text is ported for Windows, so the read declines".to_owned()],
    );
}
