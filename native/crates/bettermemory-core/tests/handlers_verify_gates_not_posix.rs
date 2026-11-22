//! `handlers_verify_gates.jsonl` has no Windows case: its paths resolve
//! through `posixpath.realpath`, and on Windows `ntpath.realpath` asks
//! the system (`_getfinalpathname`), which `fsdata` does not port: under
//! `Flavour::Windows` every gate that must resolve a path declines
//! (`Refused::NotPorted`, the crate's stated Windows gap).
//! `golden_handlers_verify_gates.rs` runs on POSIX only; here the gap is
//! recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_handlers_verify_gates_fixture_has_no_case_here() {
    common::parity::record(
        "golden_handlers_verify_gates",
        "handlers_verify_gates.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["paths resolve through ntpath on Windows, which fsdata does not port".to_owned()],
    );
}

#[test]
fn the_posix_handlers_verify_gates_python_fixture_has_no_case_here() {
    common::parity::record(
        "golden_handlers_verify_gates_python",
        "handlers_verify_gates_python.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["paths resolve through ntpath on Windows, which fsdata does not port".to_owned()],
    );
}
