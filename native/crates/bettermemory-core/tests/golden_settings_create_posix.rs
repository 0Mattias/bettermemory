//! Every case of `settings_create_posix.jsonl` against
//! `config::create::load_config_creating` on the real file system: the new
//! file's mode (0o600, the temporary file's), a directory at the path, a
//! parent or grandparent that is a file, and a parent this account cannot
//! search. POSIX only: the fixture is not generated on Windows.

#![cfg(unix)]

mod common;

use common::settings_create::run_file;

#[test]
fn settings_create_posix_cases() {
    let (checked, in_file, failures) = run_file("settings_create_posix.jsonl");
    assert!(failures.is_empty(), "{failures:#?}");
    common::assert_count("settings_create_posix.jsonl", checked, in_file);
}
