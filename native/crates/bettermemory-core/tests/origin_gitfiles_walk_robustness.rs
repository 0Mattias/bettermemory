//! Hostile input for `origin_gitfiles::walk`: a key of many pathspec
//! chains and many held directories, and a root deeper than PATH_MAX. No
//! panic, and time grows with the input, not faster.

mod common;
mod timing;

use bettermemory_core::fsdata::{Flavour, MemFs, Node, StdFs};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{Env, Host};
use bettermemory_core::origin_gitfiles::attrs::{gitattributes_signature, IndexMemo};
use bettermemory_core::origin_gitfiles::facts::Held;
use bettermemory_core::origin_gitfiles::root::ConfigMemo;
use bettermemory_core::origin_gitfiles::walk::{
    files_held, located_head, walk_files_signature, Keyed, Machine,
};

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

fn tree(n: usize) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/t", Node::Dir);
    for i in 0..n {
        fs.insert(format!("/t/d{i}").as_bytes(), Node::Dir);
    }
    fs
}

fn key(fs: &MemFs, n: usize) -> Keyed {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/t",
    };
    let mut keyed = Keyed::default();
    for i in 0..n {
        let spec = format!("d{i}/f").into_bytes();
        let chain = gitattributes_signature(&ctx, b"/t", &[&spec], Some(&mut keyed.directories))
            .expect("ported");
        keyed.chains.push((vec![spec], chain));
    }
    keyed
}

fn held(fs: &MemFs, keyed: &Keyed) -> bool {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/t",
    };
    let hung = Hung::default();
    let rule = Rule {
        ctx: &ctx,
        answers: &hung,
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let host = Host {
        euid: 0,
        owners: &StdFs,
    };
    let env = Env::default();
    let machine = Machine {
        env: &env,
        index_file_set: false,
        xdg_config_home: None,
        home: None,
    };
    files_held(
        &mut ConfigMemo::new(),
        &mut IndexMemo::new(),
        &rule,
        &host,
        &machine,
        b"/t",
        b"/t",
        keyed,
    )
    .expect("ported")
}

#[test]
fn many_chains_and_directories_hold_in_linear_time() {
    let run = |n: usize| {
        let fs = tree(n);
        let keyed = key(&fs, n);
        assert!(keyed.directories.len() > n);
        timing::once(|| assert!(held(&fs, &keyed)))
    };
    timing::grows_within(
        "files_held: 1,000 and 10,000 chains",
        timing::TENFOLD,
        &mut || run(1_000),
        &mut || run(10_000),
    );
}

#[test]
fn a_stale_directory_among_many_fails_the_check() {
    let fs = tree(2_000);
    let mut keyed = key(&fs, 2_000);
    keyed
        .directories
        .insert(b"/t/d1999".to_vec(), (-1, i128::MAX));
    assert!(!held(&fs, &keyed));
    let mut directories = Held::new();
    directories.insert(b"/nowhere".to_vec(), (i128::MIN, 0));
    let nowhere = Keyed {
        directories,
        ..Keyed::default()
    };
    assert!(!held(&fs, &nowhere));
}

#[test]
fn a_root_past_path_max_reads_without_panic() {
    let fs = tree(0);
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/t",
    };
    let hung = Hung::default();
    let rule = Rule {
        ctx: &ctx,
        answers: &hung,
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let host = Host {
        euid: 0,
        owners: &StdFs,
    };
    let env = Env::default();
    let mut deep = b"/t".to_vec();
    for _ in 0..5_000 {
        deep.extend_from_slice(b"/a");
    }
    deep.extend_from_slice(b"/\0");
    let mut directories = Held::new();
    let got = walk_files_signature(
        &mut ConfigMemo::new(),
        &mut IndexMemo::new(),
        &rule,
        &host,
        &env,
        false,
        &deep,
        Some(&mut directories),
    );
    assert_eq!(got, Ok(None));
    assert_eq!(located_head(&rule, &env, &deep), None);
}
