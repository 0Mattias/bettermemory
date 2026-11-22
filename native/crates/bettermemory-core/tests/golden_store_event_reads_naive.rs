//! Every case of `store_event_reads_naive.jsonl`: a naive `since`, which
//! the reference reads as local time, under the fixed offset the file
//! records. POSIX only: the generator sets `TZ` with `time.tzset`.

#![cfg(unix)]

mod common;

use common::parity::Ledger;

#[test]
fn every_naive_window_answers_as_the_reference() {
    let name = "store_event_reads_naive.jsonl";
    let offset = common::jsonl(name)[0]["offset"].as_i64().unwrap() as i32;
    let ledger = Ledger::new("golden_store_event_reads_naive", name);
    let (in_file, checked, failures) = common::event_reads::check_file(name, Some(offset));
    assert!(checked > 0 && checked == in_file, "{checked} of {in_file}");
    ledger.settle(checked, &failures);
}
