//! Every case of `commit_anchors_posix.jsonl` (POSIX only) against
//! `commit_anchors`: the rows that reach `ntpath.relpath` or expand a `~`
//! path, and the sweep of `relative_under`. On Windows the reference asks
//! the system there; `commit_anchors_not_posix.rs` records the gap.

#![cfg(unix)]

mod common;

#[test]
fn golden_commit_anchors_posix() {
    common::commit_anchors::every_case("golden_commit_anchors_posix", "commit_anchors_posix.jsonl");
}
