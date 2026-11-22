//! Hostile input for `origin_gitfiles::attrs`: many and deep pathspecs,
//! an index far past the first reads, a cut index whose counted entries
//! run past its end, and an index memo churned past its cap. No panic,
//! and time grows with the input, not faster.

mod common;
mod timing;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::GitDir;
use bettermemory_core::origin_gitfiles::attrs::{
    gitattributes_signature, index_gitmodules, IndexFailure, IndexMemo, INDEX_MEMO_CAP,
};
use bettermemory_core::origin_gitfiles::root::ConfigMemo;

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

fn entry(name: &[u8]) -> Vec<u8> {
    let mut out = vec![0u8; 24];
    out.extend(0o100_644u32.to_be_bytes());
    out.extend([0u8; 12]);
    out.extend([0x11; 20]);
    out.extend((name.len().min(0x0FFF) as u16).to_be_bytes());
    let pad = (out.len() + name.len() + 8) & !7;
    out.extend(name);
    out.resize(pad, 0);
    out
}

fn index(before: usize, count: u32) -> Vec<u8> {
    let mut out = b"DIRC\0\0\0\x02".to_vec();
    out.extend(count.to_be_bytes());
    for i in 0..before {
        out.extend(entry(format!(".a{i:07}").as_bytes()));
    }
    out
}

fn repo(fs: &mut MemFs, at: &str, data: Vec<u8>) -> GitDir {
    let git = format!("{at}/.git");
    fs.insert(at.as_bytes(), Node::Dir);
    fs.insert(git.as_bytes(), Node::Dir);
    fs.insert(format!("{git}/config").as_bytes(), Node::File(Vec::new()));
    fs.insert(format!("{git}/index").as_bytes(), Node::File(data));
    GitDir {
        gitdir: git.clone().into_bytes(),
        commondir: git.into_bytes(),
    }
}

fn base() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/t", Node::Dir);
    fs
}

#[test]
fn many_pathspecs_grow_linearly() {
    let fs = base();
    let run = |n: usize| {
        let specs: Vec<Vec<u8>> = (0..n).map(|i| format!("d{i}/f").into_bytes()).collect();
        let specs: Vec<&[u8]> = specs.iter().map(Vec::as_slice).collect();
        let ctx = ctx(&fs);
        timing::once(|| {
            let sig = gitattributes_signature(&ctx, b"/t", &specs, None).unwrap();
            assert_eq!(sig.map(|s| s.len()), Some(n + 1));
        })
    };
    timing::grows_within(
        "gitattributes_signature: 1,000 and 10,000 pathspecs",
        timing::TENFOLD,
        &mut || run(1_000),
        &mut || run(10_000),
    );
}

#[test]
fn a_deep_pathspec_reads_without_panic() {
    let fs = base();
    // Past PATH_MAX: the stat refuses the path and the answer is None,
    // or the chain is whole.
    let deep = vec!["x"; 5_000].join("/");
    let sig = gitattributes_signature(&ctx(&fs), b"/t", &[deep.as_bytes()], None).unwrap();
    assert!(sig.is_none_or(|s| s.len() == 5_000));
}

#[test]
fn a_large_index_grows_linearly_and_a_cut_one_is_unsettled() {
    let run = |n: usize| {
        let mut fs = base();
        let gd = repo(&mut fs, "/t/r", index(n, n as u32 + 1));
        let ctx = ctx(&fs);
        timing::once(|| {
            let (mut memo, mut config) = (IndexMemo::new(), ConfigMemo::new());
            let got = index_gitmodules(&mut memo, &mut config, &ctx, &gd, &GIT);
            assert_eq!(got, Err(IndexFailure::Unsettled));
        })
    };
    timing::grows_within(
        "index_gitmodules: 5,000 and 50,000 entries, cut short",
        timing::TENFOLD,
        &mut || run(5_000),
        &mut || run(50_000),
    );
    let mut fs = base();
    let gd = repo(&mut fs, "/t/r", index(0, u32::MAX));
    let (mut memo, mut config) = (IndexMemo::new(), ConfigMemo::new());
    assert_eq!(
        index_gitmodules(&mut memo, &mut config, &ctx(&fs), &gd, &GIT),
        Err(IndexFailure::Unsettled)
    );
}

#[test]
fn the_index_memo_stays_at_its_cap() {
    let mut fs = base();
    let gds: Vec<GitDir> = (0..1_000)
        .map(|i| repo(&mut fs, &format!("/t/r{i}"), index(0, 0)))
        .collect();
    let ctx = ctx(&fs);
    let (mut memo, mut config) = (IndexMemo::new(), ConfigMemo::new());
    for gd in &gds {
        assert_eq!(
            index_gitmodules(&mut memo, &mut config, &ctx, gd, &GIT),
            Ok(None)
        );
    }
    assert_eq!(memo.keys().len(), INDEX_MEMO_CAP);
}
