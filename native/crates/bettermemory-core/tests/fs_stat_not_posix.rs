//! `fs_stat.jsonl` has no Windows case: its trees hold links, FIFOs and
//! mode-000 folders, and Windows' `st_mode`, `st_ino` and `st_ctime` are
//! not ported (`StatResult` gives None there). Here the gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_stat_fixture_has_no_case_here() {
    for test in ["golden_fs_stat", "golden_fs_stat_stdfs"] {
        common::parity::record(
            test,
            "fs_stat.jsonl",
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["POSIX trees; Windows stat fields are not ported".to_owned()],
        );
    }
}
