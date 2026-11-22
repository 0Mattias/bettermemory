//! Every case of `store_event_reads.jsonl` against `store::event_reads`:
//! the event readers, the capture rows and the window floor over the
//! stores the file builds (mixed kinds, both stamp spellings, rows out of
//! order, a dropped index, payloads that are not objects, text that is
//! not UTF-8).

mod common;

use common::parity::Ledger;

#[test]
fn every_event_read_case_answers_as_the_reference() {
    let name = "store_event_reads.jsonl";
    let ledger = Ledger::new("golden_store_event_reads", name);
    let (in_file, checked, failures) = common::event_reads::check_file(name, None);
    assert!(checked > 0 && checked == in_file, "{checked} of {in_file}");
    ledger.settle(checked, &failures);
}
