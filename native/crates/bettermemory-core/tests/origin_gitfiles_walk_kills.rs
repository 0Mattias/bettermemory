//! Cases a plausible wrong `origin_gitfiles::walk` fails: the index slot
//! kept only while the working tree has no `.gitmodules` or the index
//! holds it unmerged, GIT_INDEX_FILE, a root without its `.git` entry,
//! the Windows flavour's decline, `files_held` passing over a part that
//! keyed nothing and failing on a moved directory, and `capture_files`
//! outside any repository.

#![cfg(unix)]

use std::path::PathBuf;

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{Env, Host};
use bettermemory_core::githead::signature::First;
use bettermemory_core::origin_gitfiles::attrs::{IndexGitmodules, IndexMemo};
use bettermemory_core::origin_gitfiles::facts::{Held, NotPorted};
use bettermemory_core::origin_gitfiles::root::ConfigMemo;
use bettermemory_core::origin_gitfiles::walk::{
    capture_files, files_held, located_head, walk_files_signature, Keyed, Machine, WalkFiles,
};

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

struct Scratch(PathBuf);

impl Scratch {
    fn new(name: &str) -> Scratch {
        let dir = std::env::temp_dir().join(format!("ogf-walk-k-{}-{name}", std::process::id()));
        std::fs::create_dir(&dir).unwrap();
        Scratch(std::fs::canonicalize(&dir).unwrap())
    }
    fn at(&self, rel: &str) -> Vec<u8> {
        self.0.join(rel).to_str().unwrap().as_bytes().to_vec()
    }
    fn write(&self, rel: &str, data: &[u8]) {
        let at = self.0.join(rel);
        std::fs::create_dir_all(at.parent().unwrap()).unwrap();
        std::fs::write(at, data).unwrap();
    }
    fn repo(&self) {
        std::fs::create_dir_all(self.0.join("r/.git/objects")).unwrap();
        self.write("r/.git/HEAD", b"ref: refs/heads/main\n");
        self.write("r/.git/config", b"[core]\n\tbare = false\n");
        self.write(
            "r/.git/refs/heads/main",
            format!("{}\n", "a".repeat(40)).as_bytes(),
        );
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn euid(s: &Scratch) -> u32 {
    use std::os::unix::fs::MetadataExt;
    std::fs::metadata(&s.0).unwrap().uid()
}

/// One stage-0 `.gitmodules` entry, or two unmerged ones.
fn index(unmerged: bool) -> Vec<u8> {
    let entry = |stage: u16| {
        let mut out = vec![0u8; 24];
        out.extend(0o100_644u32.to_be_bytes());
        out.extend([0u8; 12]);
        out.extend([0x22; 20]);
        out.extend(((stage << 12) | 11).to_be_bytes());
        let pad = (out.len() + 11 + 8) & !7;
        out.extend(b".gitmodules");
        out.resize(pad, 0);
        out
    };
    let stages: &[u16] = if unmerged { &[1, 2] } else { &[0] };
    let mut out = b"DIRC\0\0\0\x02".to_vec();
    out.extend((stages.len() as u32).to_be_bytes());
    for &s in stages {
        out.extend(entry(s));
    }
    out
}

macro_rules! setup {
    ($s:ident, $ctx:ident, $rule:ident, $host:ident, $flavour:expr) => {
        let cwd = $s.0.to_str().unwrap().as_bytes().to_vec();
        let $ctx = Ctx {
            fs: &StdFs,
            flavour: $flavour,
            cwd: &cwd,
        };
        let hung = Hung::default();
        let $rule = Rule {
            ctx: &$ctx,
            answers: &hung,
            records: &NoRecords,
            env: None,
            git: &GIT,
        };
        let $host = Host {
            euid: euid(&$s),
            owners: &StdFs,
        };
    };
}

fn walk(s: &Scratch, index_file_set: bool, root: &str) -> Result<Option<WalkFiles>, NotPorted> {
    setup!(s, ctx, rule, host, Flavour::Posix);
    let _ = &ctx;
    walk_files_signature(
        &mut ConfigMemo::new(),
        &mut IndexMemo::new(),
        &rule,
        &host,
        &Env::default(),
        index_file_set,
        &s.at(root),
        None,
    )
}

#[test]
fn the_index_slot_follows_the_working_trees_gitmodules() {
    let s = Scratch::new("slot");
    s.repo();
    s.write("r/.git/index", &index(false));
    let got = walk(&s, false, "r").unwrap().unwrap();
    assert_eq!(
        got.index,
        Some(IndexGitmodules::Entry(0o100_644, "22".repeat(20)))
    );
    assert_eq!(got.gitmodules, None);
    s.write("r/.gitmodules", b"x");
    let got = walk(&s, false, "r").unwrap().unwrap();
    assert_eq!(got.index, None);
    assert!(got.gitmodules.is_some());
    s.write("r/.git/index", &index(true));
    let got = walk(&s, false, "r").unwrap().unwrap();
    assert_eq!(got.index, Some(IndexGitmodules::Unmerged));
}

#[test]
fn git_index_file_and_a_root_without_its_entry_key_nothing() {
    let s = Scratch::new("none");
    s.repo();
    std::fs::create_dir(s.0.join("r/a")).unwrap();
    assert!(walk(&s, false, "r").unwrap().is_some());
    assert_eq!(walk(&s, true, "r"), Ok(None));
    assert_eq!(walk(&s, false, "r/a"), Ok(None));
    s.write("r/.git/shallow", b"x\n");
    assert!(walk(&s, false, "r").unwrap().unwrap().shallow.is_some());
}

#[test]
fn the_windows_flavour_checks_the_environment_before_discovery() {
    let s = Scratch::new("win");
    s.repo();
    setup!(s, ctx, rule, host, Flavour::Windows);
    let _ = &ctx;
    let walk = |index_file_set, env: &Env| {
        let mut directories = Held::new();
        let got = walk_files_signature(
            &mut ConfigMemo::new(),
            &mut IndexMemo::new(),
            &rule,
            &host,
            env,
            index_file_set,
            &s.at("r"),
            Some(&mut directories),
        );
        assert!(directories.is_empty());
        got
    };
    assert_eq!(walk(false, &Env::default()), Err(NotPorted));
    assert_eq!(walk(true, &Env::default()), Ok(None));
    let moved = Env {
        git_dir: Some(vec![]),
        ..Env::default()
    };
    assert_eq!(walk(false, &moved), Ok(None));
}

#[test]
fn files_held_reads_only_what_keyed_something() {
    let s = Scratch::new("held");
    s.repo();
    setup!(s, ctx, rule, host, Flavour::Posix);
    let _ = &ctx;
    let env = Env::default();
    let machine = Machine {
        env: &env,
        index_file_set: true,
        xdg_config_home: None,
        home: None,
    };
    let root = s.at("r");
    // A walk key with GIT_INDEX_FILE set now reads None: not held.
    let stale = Keyed {
        walk_files: Some(WalkFiles {
            config: None,
            worktree_config: None,
            gitmodules: None,
            index: None,
            shallow: None,
        }),
        ..Keyed::default()
    };
    let (mut c, mut i) = (ConfigMemo::new(), IndexMemo::new());
    let held = files_held(&mut c, &mut i, &rule, &host, &machine, &root, &root, &stale);
    assert_eq!(held, Ok(false));
    // Nothing keyed and a chain of None: held.
    let empty = Keyed {
        chains: vec![(vec![b"a".to_vec()], None)],
        ..Keyed::default()
    };
    let held = files_held(&mut c, &mut i, &rule, &host, &machine, &root, &root, &empty);
    assert_eq!(held, Ok(true));
    // A held directory that moved: not held.
    let mut directories = Held::new();
    directories.insert(root.clone(), (0, 0));
    let moved = Keyed {
        directories,
        ..Keyed::default()
    };
    let held = files_held(&mut c, &mut i, &rule, &host, &machine, &root, &root, &moved);
    assert_eq!(held, Ok(false));
}

#[test]
fn capture_outside_a_repository_and_the_head_at_a_root() {
    let s = Scratch::new("cap");
    std::fs::create_dir(s.0.join("x")).unwrap();
    s.repo();
    setup!(s, ctx, rule, host, Flavour::Posix);
    let _ = &ctx;
    let env = Env::default();
    let (sig, found) = capture_files(&rule, &host, &env, &s.at("x")).unwrap();
    assert_eq!(sig.first, First::Outside);
    assert!(found.is_none());
    let (sig, found) = capture_files(&rule, &host, &env, &s.at("r")).unwrap();
    assert_eq!(sig.first, First::GitDir(s.at("r/.git")));
    assert_eq!(found.unwrap().worktree_root, s.at("r"));
    assert_eq!(located_head(&rule, &env, &s.at("r")), Some("a".repeat(40)));
    s.write("r/.git/HEAD", b"ref: refs/tags/t\n");
    assert_eq!(located_head(&rule, &env, &s.at("r")), None);
    // An unreadable HEAD: both signatures undetermined, no repository, and
    // two captures never equal.
    s.write("r/.git/HEAD", b"junk\n");
    let (one, found) = capture_files(&rule, &host, &env, &s.at("r")).unwrap();
    assert!(matches!(one.first, First::Undetermined(_)));
    assert!(found.is_none());
    let (two, _) = capture_files(&rule, &host, &env, &s.at("r")).unwrap();
    assert_ne!(one, two);
}
