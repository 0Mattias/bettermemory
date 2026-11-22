//! Every case of `standing_reads.jsonl` against the store-reading half of
//! `standing`: each op's answer and every read made on the store and the
//! admission, in order.

mod common;
#[path = "standing_reads_world/mod.rs"]
mod world;

use common::jsonl;
use common::parity::Ledger;

#[test]
fn standing_reads_every_case() {
    let name = "standing_reads.jsonl";
    let lines = jsonl(name);
    let count = lines[0]["count"].as_u64().expect("count") as usize;
    assert_eq!(count + 1, lines.len(), "{name}: header count");
    let ledger = Ledger::new("golden_standing_reads", name);
    let mut failures = Vec::new();
    let mut checked = 0;
    for (i, row) in lines[1..].iter().enumerate() {
        let got = world::answer(row);
        if got != row["out"] {
            failures.push(format!("row {i} {}: native {got}", row["ops"]));
        }
        checked += 1;
    }
    assert!(checked > 0 && checked == count, "{name}: {checked} checked");
    ledger.settle(count, &failures);
}
