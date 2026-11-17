//! `gitdir_read.jsonl` and `gitdir_read_python.jsonl` have no Windows
//! case: their trees hold links, FIFOs and modes, and the strict walk a
//! `commondir` is resolved by is `posixpath`'s (declined on Windows). Here
//! the gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_gitdir_fixtures_have_no_case_here() {
    for (test, file) in [
        ("golden_gitdir_read", "gitdir_read.jsonl"),
        ("golden_gitdir_read_stdfs", "gitdir_read.jsonl"),
        ("golden_gitdir_read_gated", "gitdir_read_python.jsonl"),
        ("golden_gitdir_read_gated_stdfs", "gitdir_read_python.jsonl"),
    ] {
        common::parity::record(
            test,
            file,
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["POSIX trees; the strict walk is not ported on Windows".to_owned()],
        );
    }
}
