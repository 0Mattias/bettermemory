//! Every case of `store_upgrade.jsonl` against `store::upgrade`: the
//! proof on a connection alone on its store, in rollback-journal and WAL
//! mode, in the exclusive locking mode, and with its own transaction
//! open; the answer and the busy timeout, locking mode, open transaction
//! and rows left afterwards compared.

mod common;
mod store_upgrade_case;

#[test]
fn store_upgrade_every_case() {
    store_upgrade_case::every_case("golden_store_upgrade", "store_upgrade.jsonl");
}
