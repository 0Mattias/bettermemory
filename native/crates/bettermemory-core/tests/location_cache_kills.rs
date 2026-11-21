//! Cases a plausible wrong port of `location_cache` fails: the freshness
//! bounds, the token mask (and the cwd spelled ``token`` it also blanks),
//! the eviction order, an evicted HEAD or config, the discovery variables,
//! the second walk that reads HEAD, the per-worktree refs, a cwd outside
//! the root, and `forget` of one cwd.

mod common;

use bettermemory_core::capture::{Capture, Kind, State};
use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind as FileKind, MemFile, MemFs, Node, OsError, PyErr, Stat, StatResult,
    SF_DATALESS, WINDOWS_RECALL,
};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::location_cache::{
    capture, fresh, masked, signature, Asked, Cache, CwdPart, Declined, DiscoverFailed, Effects,
    Entry, Signature, CACHE_CAP, CACHE_SECONDS, UNSTAMPED,
};
use bettermemory_core::location_git::Git;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

fn outside(cwd: &[u8], id: (u64, u64)) -> Signature {
    Signature::Outside {
        cwd: CwdPart {
            path: cwd.to_vec(),
            id: Some(id),
        },
        project: None,
    }
}

#[test]
fn freshness_holds_below_the_bound_on_both_clocks_only() {
    assert!(fresh(0.0, 0.0, 0.0, 0.0));
    assert!(fresh(0.0, 0.0, CACHE_SECONDS - 1e-9, 1.0));
    assert!(!fresh(0.0, 0.0, CACHE_SECONDS, 1.0));
    assert!(!fresh(0.0, 0.0, 1.0, CACHE_SECONDS));
    assert!(!fresh(1.0, 0.0, 0.0, 0.0));
    assert!(!fresh(0.0, 1.0, 0.0, 0.0));
    assert!(!fresh(0.0, 0.0, f64::NAN, 0.0));
}

#[test]
fn the_mask_blanks_the_token_and_a_cwd_spelled_token() {
    let plain = outside(b"/w", (1, 2));
    assert_eq!(masked(&plain), plain);
    let token = outside(b"token", (1, 2));
    assert_eq!(masked(&token), outside(b"token", (1, 2)).clone_masked());
    assert_eq!(masked(&token), masked(&outside(b"token", (3, 4))));
    assert_ne!(
        masked(&outside(b"tokens", (1, 2))),
        masked(&outside(b"tokens", (3, 4)))
    );
}

trait MaskedCwd {
    fn clone_masked(&self) -> Signature;
}

impl MaskedCwd for Signature {
    fn clone_masked(&self) -> Signature {
        let Signature::Outside { cwd, project } = self else {
            unreachable!()
        };
        Signature::Outside {
            cwd: CwdPart {
                path: cwd.path.clone(),
                id: None,
            },
            project: *project,
        }
    }
}

fn entry(tag: &str) -> Entry {
    let mut cap = Capture::new(cps("/w"), State::Repo);
    cap.detail = cps(tag);
    Entry {
        capture: cap,
        signature: outside(b"/w", (1, 1)),
        monotonic: 0.0,
        wall: 0.0,
    }
}

#[test]
fn the_oldest_goes_first_and_a_put_moves_to_the_end() {
    let mut cache = Cache::default();
    for i in 0..CACHE_CAP {
        cache.put((format!("/{i}").into_bytes(), false, true), entry("x"));
    }
    cache.put((b"/0".to_vec(), false, true), entry("again"));
    cache.put((b"/new".to_vec(), false, true), entry("new"));
    let keys: Vec<&[u8]> = cache
        .entries()
        .iter()
        .map(|(k, _)| k.0.as_slice())
        .collect();
    assert_eq!(keys.len(), CACHE_CAP);
    assert_eq!(keys[0], b"/2");
    assert_eq!(keys[CACHE_CAP - 2], b"/0");
    assert_eq!(keys[CACHE_CAP - 1], b"/new");
}

fn repo_fs() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    for d in [
        &b"/r"[..],
        b"/r/.git",
        b"/r/.git/refs",
        b"/r/.git/refs/heads",
        b"/r/sub",
    ] {
        fs.insert(d, Node::Dir);
    }
    fs.insert(
        b"/r/.git/HEAD",
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(b"/r/.git/config", Node::File(b"[core]\n".to_vec()));
    fs.insert(b"/r/.git/refs/heads/main", Node::File(b"x\n".to_vec()));
    fs
}

fn git() -> Git {
    Git {
        dotgit: Some(b"/r/.git".to_vec()),
        git_dir: b"/r/.git".to_vec(),
        common_dir: b"/r/.git".to_vec(),
        root: Some(b"/r".to_vec()),
        kind: Kind::Primary,
    }
}

fn sig_of(
    fs: &MemFs,
    cwd: &[u8],
    found: Git,
    moved: bool,
) -> (Result<Option<Signature>, Declined>, Vec<bool>) {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    let mut asked = Vec::new();
    let mut discover = |_: &[u8], read_head: bool| -> Result<Option<Git>, DiscoverFailed> {
        asked.push(read_head);
        Ok(if read_head { None } else { Some(found.clone()) })
    };
    let got = signature(&ctx, cwd, false, moved, &mut discover);
    (got, asked)
}

#[test]
fn an_evicted_head_or_config_keeps_no_signature() {
    let fs = repo_fs();
    assert!(matches!(
        sig_of(&fs, b"/r", git(), false).0,
        Ok(Some(Signature::Repo(_)))
    ));
    for (path, flags) in [
        (&b"/r/.git/HEAD"[..], SF_DATALESS),
        (b"/r/.git/config", SF_DATALESS),
    ] {
        let mut fs = repo_fs();
        fs.set_flags(path, flags);
        assert_eq!(sig_of(&fs, b"/r", git(), false).0, Ok(None), "{path:?}");
    }
    let mut fs = repo_fs();
    fs.set_flags(b"/r/.git/HEAD", WINDOWS_RECALL & !SF_DATALESS);
    assert!(matches!(sig_of(&fs, b"/r", git(), false).0, Ok(Some(_))));
}

#[test]
fn a_discovery_variable_reads_nothing() {
    let (got, asked) = sig_of(&repo_fs(), b"/r", git(), true);
    assert_eq!(got, Ok(None));
    assert!(asked.is_empty());
}

#[test]
fn a_head_git_does_not_read_walks_again_reading_heads() {
    let mut fs = repo_fs();
    fs.insert(b"/r/.git/HEAD", Node::File(b"ref: heads/main\n".to_vec()));
    let (got, asked) = sig_of(&fs, b"/r", git(), false);
    assert_eq!(asked, vec![false, true]);
    assert!(matches!(got, Ok(Some(Signature::Outside { .. }))));
}

#[test]
fn per_worktree_refs_are_the_git_directorys() {
    for prefix in ["refs/bisect/", "refs/rewritten/", "refs/worktree/"] {
        let mut fs = repo_fs();
        for d in [
            &b"/r/.git/worktrees"[..],
            b"/r/.git/worktrees/w",
            b"/r/.git/worktrees/w/refs",
        ] {
            fs.insert(d, Node::Dir);
        }
        let dir = format!("/r/.git/worktrees/w/{prefix}");
        fs.insert(dir.trim_end_matches('/').as_bytes(), Node::Dir);
        fs.insert(format!("{dir}x").as_bytes(), Node::File(b"y".to_vec()));
        fs.insert(
            b"/r/.git/worktrees/w/HEAD",
            Node::File(format!("ref: {prefix}x\n").into_bytes()),
        );
        let found = Git {
            git_dir: b"/r/.git/worktrees/w".to_vec(),
            ..git()
        };
        let Ok(Some(Signature::Repo(r))) = sig_of(&fs, b"/r", found, false).0 else {
            panic!("a signature for {prefix}")
        };
        assert!(
            r.ref_part.is_some(),
            "{prefix} read under the git directory"
        );
        assert!(r.refs.is_none());
    }
}

#[test]
fn a_cwd_outside_the_root_walks_from_the_root() {
    let mut fs = repo_fs();
    fs.insert(b"/elsewhere", Node::Dir);
    fs.insert(b"/r/bettermemory.toml", Node::File(b"x".to_vec()));
    let Ok(Some(Signature::Repo(r))) = sig_of(&fs, b"/elsewhere", git(), false).0 else {
        panic!("a signature")
    };
    assert_eq!(r.inner.len(), 1);
    assert!(r.inner[0].is_some());
    let Ok(Some(Signature::Repo(r))) = sig_of(&fs, b"/r/sub", git(), false).0 else {
        panic!("a signature")
    };
    assert_eq!(r.inner.len(), 2);
    assert!(r.inner[0].is_none() && r.inner[1].is_some());
}

#[test]
fn a_recorded_elsewhere_forgets_its_cwd_only() {
    let mut cache = Cache::default();
    cache.put((b"/other".to_vec(), false, true), entry("other"));
    cache.put((b"/w".to_vec(), true, true), entry("w-token"));
    let mut sig = |_: &[u8]| Ok(Some(outside(b"/w", (1, 1))));
    let mut clocks = || (1.0, 1.0);
    let mut unchanged = |_: &Capture| Ok(true);
    let mut calls = 0;
    let mut uncached = |_: &[u8], _: bool, _: f64, _: bool| {
        calls += 1;
        let mut cap = Capture::new(cps("/w"), State::Repo);
        cap.kept_apart = true;
        cap.git_dir = Some(cps("/g"));
        cap.root = Some(cps("/w"));
        cap
    };
    let mut record = |_: &PyStr, _: &PyStr| "elsewhere".to_owned();
    let mut fx = Effects {
        signature: &mut sig,
        clocks: &mut clocks,
        config_unchanged: &mut unchanged,
        uncached: &mut uncached,
        record_working_tree: &mut record,
    };
    let got = capture(&mut cache, &mut fx, b"/w", Asked::new(false)).unwrap();
    assert!(!got.tree_recorded);
    let keys: Vec<&[u8]> = cache
        .entries()
        .iter()
        .map(|(k, _)| k.0.as_slice())
        .collect();
    assert_eq!(keys, vec![&b"/other"[..]]);
    assert_eq!(calls, 2);
}

/// `_stamp` answers `(-1, -1, -1, -1, -1)` where githead's stamp raises (a
/// link loop is ELOOP, an OSError), never None as for a missing file.
#[test]
fn a_stamp_that_raises_is_unstamped_not_missing() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w", Node::Dir);
    fs.insert(
        b"/w/bettermemory.toml",
        Node::Link(b"/w/bettermemory.toml".to_vec()),
    );
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    let mut discover = |_: &[u8], _: bool| -> Result<Option<Git>, DiscoverFailed> { Ok(None) };
    let got = signature(&ctx, b"/w", false, false, &mut discover).unwrap();
    let Some(Signature::Outside { project, .. }) = got else {
        panic!("outside: {got:?}")
    };
    assert_eq!(project, Some(UNSTAMPED));
}

/// A file system whose `os.stat` of `at` raises `fault`.
struct StatFault {
    fs: MemFs,
    at: &'static [u8],
    fault: OsError,
}

impl Fs for StatFault {
    type File = MemFile;
    fn open(&self, path: &[u8]) -> Result<MemFile, PyErr> {
        self.fs.open(path)
    }
    fn fstat(&self, file: &mut MemFile) -> Result<Stat, PyErr> {
        self.fs.fstat(file)
    }
    fn read(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, PyErr> {
        self.fs.read(file, max)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.fs.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.fs.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<FileKind, PyErr> {
        self.fs.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.fs.readlink(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        if path == self.at {
            return Err(self.fault);
        }
        self.fs.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.fs.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.fs.listdir(path)
    }
}

/// A FileNotFoundError on config is a missing config only where
/// `path_missing` reads it as gone; a share that does not answer
/// (ERROR_BAD_NETPATH, ENG-217) keeps no signature.
#[test]
fn a_config_not_found_that_is_not_gone_keeps_no_signature() {
    let plain = OsError::new(PyErr::FileNotFound, "ENOENT");
    let sig = |fault: OsError| {
        let fs = StatFault {
            fs: repo_fs(),
            at: b"/r/.git/config",
            fault,
        };
        let ctx = Ctx {
            fs: &fs,
            flavour: Flavour::Posix,
            cwd: b"/",
        };
        let mut discover = |_: &[u8], read_head: bool| -> Result<Option<Git>, DiscoverFailed> {
            Ok((!read_head).then(git))
        };
        signature(&ctx, b"/r", false, false, &mut discover)
    };
    let Ok(Some(Signature::Repo(r))) = sig(plain) else {
        panic!("a signature")
    };
    assert_eq!(r.config, None);
    let share = OsError {
        winerror: Some(53),
        ..plain
    };
    assert_eq!(sig(share), Ok(None));
}
