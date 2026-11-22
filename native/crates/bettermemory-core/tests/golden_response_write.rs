//! Every case of `response_write.jsonl` (not gated) against the native
//! `response_write`. The spelling is the generator's
//! (`tests/contract/gen_response_write.py`).

mod common;

const FILE: &str = "response_write.jsonl";

#[test]
fn golden_response_write() {
    common::response_write::check(FILE, "golden_response_write");
}
