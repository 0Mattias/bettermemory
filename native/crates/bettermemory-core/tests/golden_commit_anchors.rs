//! Every case of `commit_anchors.jsonl` (not gated) against
//! `commit_anchors`: `relative_under`, `expected_home` and
//! `project_commit_anchors` over the generator's anchors, on the rows that
//! answer the same on every host (`gen_commit_anchors.py`).

mod common;

#[test]
fn golden_commit_anchors() {
    common::commit_anchors::every_case("golden_commit_anchors", "commit_anchors.jsonl");
}
