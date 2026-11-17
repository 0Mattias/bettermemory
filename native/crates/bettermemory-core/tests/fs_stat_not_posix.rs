//! `fs_stat.jsonl` has no Windows case: its trees hold links, FIFOs and
//! mode-000 folders, and Windows' `st_mode`, `st_ino` and `st_ctime` are
//! not ported (`StatResult` gives None there). `fs_access.jsonl` neither:
//! links, FIFOs, modes and `O_NOFOLLOW`, which Windows' `os` lacks. Here
//! the gaps are recorded.

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

#[test]
fn the_posix_access_fixture_has_no_case_here() {
    for test in ["golden_fs_access", "golden_fs_access_stdfs"] {
        common::parity::record(
            test,
            "fs_access.jsonl",
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &[
                "POSIX trees; Windows has no O_NOFOLLOW and answers access by attributes"
                    .to_owned(),
            ],
        );
    }
}

#[test]
fn the_posix_samestat_fixture_has_no_case_here() {
    for test in ["golden_fs_samestat", "golden_fs_samestat_stdfs"] {
        common::parity::record(
            test,
            "fs_samestat.jsonl",
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["POSIX trees; Windows device and inode are not ported".to_owned()],
        );
    }
}
