//! Hostile input for `origin_gitfiles::root`: a config of the limit's
//! size and one past it, a memo churned far past its cap, and the
//! not-a-directory memo fed thousands of paths. No panic, and the memo's
//! cost grows with the work, not with what it has seen.

mod common;
mod timing;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::origin_gitfiles::root::{
    config_facts, ConfigMemo, NotADirectorySeen, CONFIG_LIMIT, CONFIG_MEMO_CAP, NOT_A_DIRECTORY_CAP,
};

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

fn ctx(fs: &MemFs) -> Ctx<'_, MemFs> {
    Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/t",
    }
}

#[test]
fn a_config_at_and_past_the_limit_reads_without_panic() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/t", Node::Dir);
    let mut at = b"[core]\n".to_vec();
    at.resize(CONFIG_LIMIT, b'#');
    let mut past = at.clone();
    past.push(b'#');
    fs.insert(b"/t/at", Node::File(at));
    fs.insert(b"/t/past", Node::File(past));
    let ctx = ctx(&fs);
    let mut memo = ConfigMemo::new();
    assert!(config_facts(&mut memo, &ctx, b"/t/at", &GIT)
        .unwrap()
        .is_some());
    assert!(config_facts(&mut memo, &ctx, b"/t/past", &GIT)
        .unwrap()
        .is_none());
    assert_eq!(memo.len(), 1);
}

#[test]
fn a_memo_churned_past_its_cap_stays_at_the_cap() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/t", Node::Dir);
    let names: Vec<String> = (0..1_000).map(|i| format!("/t/c{i}")).collect();
    for name in &names {
        fs.insert(
            name.as_bytes(),
            Node::File(b"[core]\n\tbare = false\n".to_vec()),
        );
    }
    let ctx = ctx(&fs);
    let mut memo = ConfigMemo::new();
    for name in &names {
        config_facts(&mut memo, &ctx, name.as_bytes(), &GIT).unwrap();
        assert!(memo.len() <= CONFIG_MEMO_CAP);
    }
    assert_eq!(memo.len(), CONFIG_MEMO_CAP);
    assert_eq!(memo.keys().last().unwrap(), names[999].as_bytes());
}

#[test]
fn the_not_a_directory_memo_costs_per_call_not_per_path_seen() {
    let run = |n: usize| {
        let mut seen = NotADirectorySeen::new();
        let mut firsts = 0usize;
        for i in 0..n {
            if seen.first(format!("/gone/{}", i % (NOT_A_DIRECTORY_CAP * 2)).as_bytes()) {
                firsts += 1;
            }
        }
        firsts
    };
    // Cycling over twice the cap, every path is first again: the oldest
    // went out before it came back.
    assert_eq!(run(1_000), 1_000);
    timing::grows_within(
        "first_not_a_directory: 2,000 and 20,000 calls",
        timing::TENFOLD,
        &mut || timing::once(|| run(2_000)),
        &mut || timing::once(|| run(20_000)),
    );
}
