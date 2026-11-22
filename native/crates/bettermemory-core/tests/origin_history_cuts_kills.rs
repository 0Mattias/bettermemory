//! Cases a plausible wrong `cuts_in` fails: a path through a regular file
//! (NotADirectoryError: None, not the empty set a `missing()` test would
//! give), a short read loop and reads that stop short of the cap, the width
//! from the suffix, a lowercase that reaches past ASCII, the parent's
//! spelling, and the Windows flavour.

use std::collections::BTreeSet;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFs, Node, OsError, PyErr, Stat, StatResult,
};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::origin_history::{cuts_in, CUTS_MAX};
use bettermemory_core::pyvalue::PyStr;

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};
const A: &[u8] = b"0123456789abcdef0123456789abcdef01234567";
const SHA256: &[u8] =
    b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n";

fn fs(flavour: Flavour, files: &[(&str, Node)]) -> MemFs {
    let mut fs = MemFs::new(flavour);
    fs.insert(b"/w", Node::Dir);
    for (path, node) in files {
        fs.insert(path.as_bytes(), node.clone());
    }
    fs
}

fn run(fs: &MemFs, flavour: Flavour, path: &[u8]) -> Result<Option<BTreeSet<PyStr>>, Failure> {
    let ctx = Ctx {
        fs,
        flavour,
        cwd: b"/w",
    };
    cuts_in(
        &Rule {
            ctx: &ctx,
            answers: &Hung::default(),
            records: &NoRecords,
            env: None,
            git: &GIT,
        },
        path,
    )
}

fn one(oid: &[u8]) -> Option<BTreeSet<PyStr>> {
    Some([oid.iter().map(|&b| u32::from(b)).collect()].into())
}

#[test]
fn a_path_through_a_file_is_none_on_posix() {
    let f = fs(Flavour::Posix, &[("/w/file", Node::File(b"x".to_vec()))]);
    assert_eq!(run(&f, Flavour::Posix, b"file/shallow"), Ok(None));
}

#[test]
fn missing_is_the_empty_set() {
    let f = fs(Flavour::Posix, &[]);
    assert_eq!(
        run(&f, Flavour::Posix, b"/w/shallow"),
        Ok(Some(BTreeSet::new()))
    );
}

#[test]
fn a_directory_is_none() {
    let f = fs(Flavour::Posix, &[("/w/repo", Node::Dir)]);
    assert_eq!(run(&f, Flavour::Posix, b"repo"), Ok(None));
}

#[test]
fn exactly_the_cap_is_read_whole() {
    let mut data = A.to_vec();
    data.resize(CUTS_MAX, b'0');
    let f = fs(Flavour::Posix, &[("/w/shallow", Node::File(data))]);
    assert_eq!(run(&f, Flavour::Posix, b"shallow"), Ok(one(A)));
}

/// A file system whose reads hand over at most 4096 bytes a call, as a
/// pipe or a slow disk may: the loop must read past the cap to see the
/// extra byte.
struct ShortReads(MemFs);

impl Fs for ShortReads {
    type File = <MemFs as Fs>::File;
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.0.open(path)
    }
    fn open_waiting(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.0.open_waiting(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.0.open_result(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.0.fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        self.0.read(file, max.min(4096))
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.0.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.0.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.0.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.0.readlink(path)
    }
    fn readlink_result(&self, path: &[u8]) -> Result<Vec<u8>, OsError> {
        self.0.readlink_result(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.0.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.0.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.0.listdir(path)
    }
}

fn run_short(files: &[(&str, Node)]) -> Result<Option<BTreeSet<PyStr>>, Failure> {
    let short = ShortReads(fs(Flavour::Posix, files));
    let ctx = Ctx {
        fs: &short,
        flavour: Flavour::Posix,
        cwd: b"/w",
    };
    cuts_in(
        &Rule {
            ctx: &ctx,
            answers: &Hung::default(),
            records: &NoRecords,
            env: None,
            git: &GIT,
        },
        b"shallow",
    )
}

#[test]
fn short_reads_past_the_cap_are_none() {
    let mut data = A.to_vec();
    data.resize(CUTS_MAX + 1, b'0');
    assert_eq!(run_short(&[("/w/shallow", Node::File(data))]), Ok(None));
    let mut data = A.to_vec();
    data.resize(CUTS_MAX, b'0');
    assert_eq!(run_short(&[("/w/shallow", Node::File(data))]), Ok(one(A)));
}

#[test]
fn an_endless_file_is_none() {
    let f = fs(Flavour::Posix, &[("/w/shallow", Node::Endless)]);
    assert_eq!(run(&f, Flavour::Posix, b"shallow"), Ok(None));
}

#[test]
fn the_width_is_the_configs_not_the_suffixes() {
    let long = [A, A].concat();
    let f = fs(
        Flavour::Posix,
        &[
            ("/w/shallow", Node::File(long[..64].to_vec())),
            ("/w/config", Node::File(b"".to_vec())),
        ],
    );
    assert_eq!(run(&f, Flavour::Posix, b"shallow"), Ok(one(A)));
    let f = fs(
        Flavour::Posix,
        &[
            ("/w/shallow", Node::File(long[..64].to_vec())),
            ("/w/config", Node::File(SHA256.to_vec())),
        ],
    );
    assert_eq!(run(&f, Flavour::Posix, b"shallow"), Ok(one(&long[..64])));
}

#[test]
fn non_ascii_is_not_hex() {
    let mut data = A.to_vec();
    data[0] = 0xc3;
    let f = fs(Flavour::Posix, &[("/w/shallow", Node::File(data))]);
    assert_eq!(run(&f, Flavour::Posix, b"shallow"), Ok(None));
}

#[test]
fn the_parent_is_read_as_path_spells_it() {
    // `repo/shallow/` opens `repo/shallow`, and the config is `repo/config`.
    let f = fs(
        Flavour::Posix,
        &[
            ("/w/repo", Node::Dir),
            ("/w/repo/shallow", Node::File(A.to_vec())),
            (
                "/w/repo/config",
                Node::File(b"[core]\n\trepositoryformatversion = 2\n".to_vec()),
            ),
        ],
    );
    assert_eq!(run(&f, Flavour::Posix, b"repo/shallow/"), Ok(None));
    assert_eq!(run(&f, Flavour::Posix, b"/w/repo//shallow"), Ok(None));
}

#[test]
fn a_nul_raises_before_any_open() {
    let f = fs(Flavour::Posix, &[]);
    assert_eq!(run(&f, Flavour::Posix, b"a\0b"), Err(Failure::Value));
}

#[test]
fn the_windows_flavour_declines() {
    let f = fs(Flavour::Windows, &[]);
    assert_eq!(
        run(&f, Flavour::Windows, b"C:\\w\\shallow"),
        Err(Failure::NotPorted)
    );
}
