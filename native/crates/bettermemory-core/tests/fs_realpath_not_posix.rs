//! `fs_realpath.jsonl` and `fs_realpath_python.jsonl` have no Windows
//! case: their trees hold links and FIFOs and their answers are
//! `posixpath`'s, and the Windows flavour of `realpath` is not ported (it
//! answers `Unresolved::NotPorted`). Here the gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_realpath_fixtures_have_no_case_here() {
    for (test, file) in [
        ("golden_fs_realpath", "fs_realpath.jsonl"),
        ("golden_fs_realpath_stdfs", "fs_realpath.jsonl"),
        ("golden_fs_realpath_loops", "fs_realpath_python.jsonl"),
        ("golden_fs_realpath_loops_stdfs", "fs_realpath_python.jsonl"),
    ] {
        common::parity::record(
            test,
            file,
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["POSIX trees and posixpath's answers; Windows realpath is not ported".to_owned()],
        );
    }
}
