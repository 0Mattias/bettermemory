//! Every case of `store_refusal.jsonl` against `store_refusal`: manifests
//! of every shape beside a store, records of every shape beside a free
//! store, read through the directory lock and through the record branch.

mod common;
mod store_refusal_case;

#[test]
fn every_case_answers_as_the_reference() {
    store_refusal_case::run("store_refusal.jsonl", "golden");
}
