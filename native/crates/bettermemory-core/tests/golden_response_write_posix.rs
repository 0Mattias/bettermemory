//! Every case of `response_write_posix.jsonl` (POSIX only) against the
//! native `response_write`: origins whose fields hold a surrogate, which
//! `os.fsencode` encodes with `surrogateescape` on POSIX.

#![cfg(unix)]

mod common;

#[test]
fn golden_response_write_posix() {
    common::response_write::check("response_write_posix.jsonl", "golden_response_write_posix");
}
