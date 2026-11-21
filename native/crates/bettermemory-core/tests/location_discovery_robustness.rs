//! Hostile inputs to `location_discovery`: a start thousands of parts
//! deep, a walk over a hundred thousand directories, very long and
//! NUL-holding `GIT_DIR` and `GIT_WORK_TREE` values. No panic, and the
//! walk's time grows linearly with its length.
//!
//! The times are printed beside the reference's, measured with CPython
//! 3.13 on macOS arm64: `_discover` with a 1 MiB `GIT_DIR` takes 3.7 ms
//! and from a start of 20,000 parts 0.2 ms, both raising `GitDies`. A
//! `GIT_DIR` holding a NUL has no reference counterpart: `os.environ`
//! refuses it with ValueError, so the process never holds one.

mod timing;

use std::time::Duration;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::location_discovery::{discover, Env};

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

fn walk(fs: &MemFs, start: &[u8], dirs: usize, env: Env<'_>) -> Duration {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let mut refused = Vec::new();
    timing::once(|| {
        let got = discover(
            &rule,
            &env,
            start,
            Some(true),
            |_: &[u8], _: Option<u64>| Ok((0..dirs).map(|i| Ok(format!("/d{i}").into_bytes()))),
            Some(&mut refused),
            true,
        );
        std::hint::black_box(got)
    })
}

#[test]
fn a_deep_start_and_long_values_do_not_panic() {
    let fs = MemFs::new(Flavour::Posix);
    let deep = b"/a".repeat(20_000);
    let took = walk(&fs, &deep, 1, Env::default());
    eprintln!("start of 20,000 parts: {took:?} (reference 0.2 ms)");
    let long = vec![b'x'; 1 << 20];
    let nul = b"a\0b".to_vec();
    let g = b"/g".to_vec();
    for value in [&long, &nul, &deep] {
        for (git_dir, work_tree) in [(value, None), (&g, Some(value))] {
            let env = Env {
                git_dir: Some(git_dir),
                work_tree: work_tree.map(|v| &v[..]),
            };
            let took = walk(&fs, &deep, 0, env);
            let what = if work_tree.is_none() {
                "GIT_DIR"
            } else {
                "GIT_WORK_TREE"
            };
            let reference = if work_tree.is_none() && value.len() == 1 << 20 {
                " (reference 3.7 ms)"
            } else {
                ""
            };
            eprintln!("{what} of {} bytes: {took:?}{reference}", value.len());
        }
    }
}

#[test]
fn a_walk_of_many_directories_grows_linearly() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/s", Node::Dir);
    timing::grows_within(
        "location_discovery walk",
        timing::TENFOLD,
        &mut || walk(&fs, b"/s", 10_000, Env::default()),
        &mut || walk(&fs, b"/s", 100_000, Env::default()),
    );
}
