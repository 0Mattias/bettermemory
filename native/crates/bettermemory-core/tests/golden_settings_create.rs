//! Every case of `settings_create.jsonl` against
//! `config::create::load_config_creating` on the real file system: the
//! first-run write over a missing file, a missing parent chain and a file
//! that is there, and `DEFAULT_CONFIG` against the recorded length and
//! sha256.

mod common;

use bettermemory_core::config::create::DEFAULT_CONFIG;
use common::settings_create::{run_file, sha256_hex};

#[test]
fn default_config_is_the_reference_text() {
    let rows = common::jsonl("settings_create.jsonl");
    let default = rows
        .iter()
        .find_map(|r| r.get("default"))
        .expect("the default row");
    assert_eq!(
        default["length"].as_u64().unwrap() as usize,
        DEFAULT_CONFIG.len()
    );
    assert_eq!(
        default["sha256"].as_str().unwrap(),
        sha256_hex(DEFAULT_CONFIG.as_bytes())
    );
}

#[test]
fn settings_create_cases() {
    let (checked, in_file, failures) = run_file("settings_create.jsonl");
    assert!(failures.is_empty(), "{failures:#?}");
    common::assert_count("settings_create.jsonl", checked, in_file);
}
