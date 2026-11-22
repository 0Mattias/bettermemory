//! The gap `project_file_parse_posix.jsonl` leaves off POSIX: the file is
//! POSIX only (a FIFO, links, a mode-000 file and the C library's texts),
//! so no Windows case pins `parse` over a real tree. Class os: under the
//! Windows flavour `StdFs` words no OS error (`Fs::strerror` is None), so
//! an unreadable file declines (`Fault::NotPorted`) where the reference
//! words the C runtime's text.

#![cfg(not(unix))]

use bettermemory_core::fsdata::{Flavour, PyErr, StdFs};
use bettermemory_core::gitfile_rule::hostpath::text::Process;
use bettermemory_core::project_file::{parse, Fault, Host};
use bettermemory_core::pyvalue::PyStr;

struct Cwd;

impl Process for Cwd {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        Ok("C:\\".chars().map(u32::from).collect())
    }
}

#[test]
fn an_unreadable_file_declines_under_windows() {
    let host = Host {
        fs: &StdFs,
        flavour: Flavour::Windows,
        process: &Cwd,
        darwin: false,
    };
    let path: PyStr = "C:\\no\\such\\bettermemory.toml"
        .chars()
        .map(u32::from)
        .collect();
    assert_eq!(parse(&host, &path), Err(Fault::NotPorted));
}
