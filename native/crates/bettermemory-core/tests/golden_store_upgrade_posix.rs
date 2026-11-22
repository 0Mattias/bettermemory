//! Every case of `store_upgrade_posix.jsonl` against `store::upgrade`: the
//! proof with a second connection holding the store open, a read, a
//! reserved or an exclusive lock, in rollback-journal and WAL mode. POSIX
//! only, as the fixture is; `store_upgrade_not_posix.rs` records the gap.

#![cfg(unix)]

mod common;
mod store_upgrade_case;

#[test]
fn store_upgrade_posix_every_case() {
    store_upgrade_case::every_case("golden_store_upgrade_posix", "store_upgrade_posix.jsonl");
}
