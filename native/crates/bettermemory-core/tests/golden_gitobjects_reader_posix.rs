//! Every case of `gitobjects_reader_posix.jsonl` (POSIX only) against
//! `gitobjects::files::reader`: a FIFO at a loose path, unreadable files,
//! an unlistable fanout and objects in an alternate, in memory and on
//! disk.

#![cfg(unix)]

mod common;

#[test]
fn gitobjects_reader_posix_every_case() {
    common::gitobjects_reader::every_case(
        "golden_gitobjects_reader_posix",
        "gitobjects_reader_posix.jsonl",
    );
}
