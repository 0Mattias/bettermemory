//! Every case of `store_refusal_posix.jsonl` against `store_refusal`:
//! a restore holding the store's directory or its record file, the
//! record branch with a lock held, an unreadable record, a FIFO and links
//! as the manifest, hard links, a store reached through a symbolic link,
//! a directory as the store and `..` past a missing directory. POSIX
//! only: `store_refusal_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;
mod store_refusal_case;

#[test]
fn every_posix_case_answers_as_the_reference() {
    store_refusal_case::run("store_refusal_posix.jsonl", "posix");
}
