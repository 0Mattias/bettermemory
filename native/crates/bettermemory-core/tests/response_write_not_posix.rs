//! `response_write_posix.jsonl` has no Windows case: `os.fsencode`
//! encodes a surrogate with `surrogatepass` there, so the bytes of each
//! `<field>_bytes_base64` follow the system. `golden_response_write_posix.rs`
//! runs on POSIX only; here the gap is recorded. The Windows branch
//! (`FsEncoding::SurrogatePass`) is tested in `response_write_kills.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_response_write_fixture_has_no_case_here() {
    common::parity::record(
        "golden_response_write_posix",
        "response_write_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["os.fsencode uses surrogatepass on Windows; the fixture is POSIX only".to_owned()],
    );
}
