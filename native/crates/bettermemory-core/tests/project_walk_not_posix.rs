//! `project_walk.jsonl` has no Windows case: its generator makes links
//! and a mode-000 folder, holds strerror texts and spells paths as the
//! host's `Path` does. `golden_project_walk.rs` runs on POSIX only; here
//! the gap is recorded. `project_walk_kills.rs` and
//! `project_walk_robustness.rs` run everywhere, the Windows flavour among
//! their cases. Under the Windows flavour on a Windows host `abspath`
//! asks `nt._getfullpathname`, which the crate does not port (ENG-124): a
//! caller hands in a `Process` that answers it.

#![cfg(not(unix))]

mod common;

#[test]
fn the_project_walk_fixture_has_no_case_here() {
    common::parity::record(
        "golden_project_walk",
        "project_walk.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes links and a mode-000 folder; Windows has no case".to_owned()],
    );
}
