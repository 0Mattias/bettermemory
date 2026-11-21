//! Unit 4 of `origin_gitfiles.py`: the per-stamp memo of config facts
//! (`_CONFIG_LIMIT`, `_CONFIG_MEMO`, `_fd_stamp`, `_config_facts`),
//! whether a repository's config may move its working tree
//! (`_WORKTREE_CONFIG_EXTENSION`, `_worktree_config_facts`,
//! `_config_moves_worktree`), the root read from the files where
//! `git rev-parse` exited non-zero (`refused_worktree_root`) and the
//! once-per-path memo (`first_not_a_directory`).
//!
//! The reference keeps both memos in process-wide dicts behind a lock;
//! here they are values the caller holds ([`ConfigMemo`],
//! [`NotADirectorySeen`]).

use std::collections::HashSet;

use super::facts::{facts_of, no_config, ConfigFacts, NotPorted};
use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{Flavour, Fs, PyErr, Refused, StatResult};
use crate::gitconfig::legacy::Stamp;
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::discovery::Rule;
use crate::gitfile_rule::follow::Records;
use crate::gitfile_rule::format::GitFacts;
use crate::gitfile_rule::gitdir::{Ctx, Failure};
use crate::gitfile_rule::hostpath::join;
use crate::gitfile_rule::probe::{path_missing, Raised};
use crate::githead::discovery::{locate, stamp, Env};
use crate::githead::GitDir;

use super::machine::spelled_as_git;

/// `_CONFIG_LIMIT`: the most bytes of a config file read.
pub const CONFIG_LIMIT: usize = 1 << 20;

/// `_CONFIG_MEMO_CAP`: the most files whose facts are kept.
pub const CONFIG_MEMO_CAP: usize = 64;

/// `_WORKTREE_CONFIG_EXTENSION`.
pub const WORKTREE_CONFIG_EXTENSION: &[u8] = b"extensions.worktreeconfig";

/// `_NOT_A_DIRECTORY_CAP`.
pub const NOT_A_DIRECTORY_CAP: usize = 64;

/// What `_config_facts` answers when it answers: the file's stamp (None
/// where it does not exist) and its facts.
pub type ConfigRead = (Option<Stamp>, ConfigFacts);

/// `_CONFIG_MEMO`: the facts of each file read, keyed on the path as
/// given, oldest first, a hit needing the stamp alone, as the reference
/// keys it: facts kept under one git on PATH are reused under another
/// until the file's stamp changes or the memo is cleared.
#[derive(Clone, Debug, Default)]
pub struct ConfigMemo {
    entries: Vec<(Vec<u8>, Stamp, ConfigFacts)>,
}

impl ConfigMemo {
    pub fn new() -> ConfigMemo {
        ConfigMemo::default()
    }

    /// `_clear_config_memo`.
    pub fn clear(&mut self) {
        self.entries.clear();
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// The keys held, oldest first.
    pub fn keys(&self) -> Vec<Vec<u8>> {
        self.entries.iter().map(|e| e.0.clone()).collect()
    }

    fn hit(&mut self, key: &[u8], current: &Stamp) -> Option<ConfigRead> {
        let at = self.entries.iter().position(|e| e.0 == key)?;
        if self.entries[at].1 != *current {
            return None;
        }
        let entry = self.entries.remove(at);
        let read = (Some(entry.1), entry.2.clone());
        self.entries.push(entry);
        Some(read)
    }

    fn keep(&mut self, key: &[u8], stamp: Stamp, facts: ConfigFacts) {
        self.entries.retain(|e| e.0 != key);
        self.entries.push((key.to_vec(), stamp, facts));
        while self.entries.len() > CONFIG_MEMO_CAP {
            self.entries.remove(0);
        }
    }
}

/// `_fd_stamp(fd)`: the five fields of an open file's `os.fstat`; not
/// ported where a field is not (Windows' change time, inode, mode).
fn fd_stamp(st: &StatResult) -> Result<Stamp, NotPorted> {
    let (Some(ctime), Some(ino), Some(mode)) = (st.ctime_ns, st.ino, st.mode) else {
        return Err(NotPorted);
    };
    Ok([
        st.mtime_ns,
        ctime,
        i128::from(st.size),
        i128::from(ino),
        i128::from(mode),
    ])
}

const S_IFMT: i128 = 0o170_000;
const S_IFREG: i128 = 0o100_000;

/// `_config_facts(path)`: `Ok(None)` where the reference answers None (the
/// file cannot be read, is not a regular file or is larger than
/// [`CONFIG_LIMIT`]), `(None, no_config())` where nothing is there, else
/// the stamp taken from the open file before the read and the facts.
/// Kept in `memo` only where the stamps before and after the read agree.
/// `NotPorted` where the file system gives no full stamp.
///
/// The reference closes the file in a `finally` and lets an OSError of
/// `os.close` out; the port reads a failed close as nothing, since the
/// read is already whole.
pub fn config_facts<F: Fs + ?Sized>(
    memo: &mut ConfigMemo,
    ctx: &Ctx<'_, F>,
    path: &[u8],
    git: &GitFacts,
) -> Result<Option<ConfigRead>, NotPorted> {
    let current = match stamp(ctx, path, true) {
        Ok(Some(current)) => current,
        Ok(None) => return Ok(Some((None, no_config()))),
        Err(Failure::Os(_) | Failure::Value) => return Ok(None),
        Err(_) => return Err(NotPorted),
    };
    if let Some(kept) = memo.hit(path, &current) {
        return Ok(Some(kept));
    }
    let Ok(at) = ctx.at(path) else {
        return Ok(None);
    };
    let mut file = match ctx.fs.open_result(&at) {
        Ok(file) => file,
        Err(e) => {
            if e.class == PyErr::FileNotFound && path_missing(Raised::from(e)) {
                return Ok(Some((None, no_config())));
            }
            return Ok(None);
        }
    };
    let read = read_open(ctx.fs, &mut file);
    let _ = ctx.fs.close(file);
    let Some((before, after, data)) = read? else {
        return Ok(None);
    };
    if data.len() > CONFIG_LIMIT {
        return Ok(None);
    }
    let facts = facts_of(&data, git);
    if before == after {
        memo.keep(path, before, facts.clone());
    }
    Ok(Some((Some(before), facts)))
}

/// The stamps before and after and the bytes read (up to one past the
/// limit); None where the reference answers None.
#[allow(clippy::type_complexity)]
fn read_open<F: Fs + ?Sized>(
    fs: &F,
    file: &mut F::File,
) -> Result<Option<(Stamp, Stamp, Vec<u8>)>, NotPorted> {
    let before = match fs.fstat_full(file).ok_or(NotPorted)? {
        Ok(st) => fd_stamp(&st)?,
        Err(_) => return Ok(None),
    };
    if before[4] & S_IFMT != S_IFREG {
        return Ok(None);
    }
    let mut data: Vec<u8> = Vec::new();
    while data.len() <= CONFIG_LIMIT {
        match fs.read_once(file, CONFIG_LIMIT + 1 - data.len()) {
            Ok(chunk) if chunk.is_empty() => break,
            Ok(chunk) => data.extend_from_slice(&chunk),
            Err(_) => return Ok(None),
        }
    }
    let after = match fs.fstat_full(file).ok_or(NotPorted)? {
        Ok(st) => fd_stamp(&st)?,
        Err(_) => return Ok(None),
    };
    Ok(Some((before, after, data)))
}

fn joined(flavour: Flavour, dir: &[u8], name: &[u8]) -> Vec<u8> {
    join(flavour, dir, name)
}

/// `_worktree_config_facts(gd, common)`: `config.worktree`'s read where
/// the common config sets `extensions.worktreeConfig`, else nothing to
/// key on.
pub fn worktree_config_facts<F: Fs + ?Sized>(
    memo: &mut ConfigMemo,
    ctx: &Ctx<'_, F>,
    gd: &GitDir,
    common: &ConfigFacts,
    git: &GitFacts,
) -> Result<Option<ConfigRead>, NotPorted> {
    match &common.names {
        Some(names) if names.contains(WORKTREE_CONFIG_EXTENSION) => {}
        _ => return Ok(Some((None, no_config()))),
    }
    let path = joined(ctx.flavour, &gd.gitdir, b"config.worktree");
    config_facts(memo, ctx, &path, git)
}

/// `_config_moves_worktree(gd)`: whether the common config or
/// config.worktree could make git name another working tree: a file
/// that cannot be read, that git refuses or that holds a NUL byte,
/// `core.worktree`, a `bare` line other than a plain false, or a value
/// `git rev-parse` may die on.
pub fn config_moves_worktree<F: Fs + ?Sized>(
    memo: &mut ConfigMemo,
    ctx: &Ctx<'_, F>,
    gd: &GitDir,
    git: &GitFacts,
) -> Result<bool, NotPorted> {
    let paths = [
        joined(ctx.flavour, &gd.commondir, b"config"),
        joined(ctx.flavour, &gd.gitdir, b"config.worktree"),
    ];
    for path in &paths {
        let Some((_, facts)) = config_facts(memo, ctx, path, git)? else {
            return Ok(true);
        };
        if facts
            .names
            .as_ref()
            .is_none_or(|names| names.contains(&b"core.worktree"[..]))
            || facts.bare_moves
            || facts.git_dies
        {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Why `refused_worktree_root` gives no answer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RootFault {
    /// Not ported here (the Windows flavour, a file system without a full
    /// stamp or without `F_GETPATH` on macOS): a caller declines.
    NotPorted,
    /// The reference lets a ValueError out (a NUL in the spelled root).
    Value,
}

impl From<NotPorted> for RootFault {
    fn from(_: NotPorted) -> RootFault {
        RootFault::NotPorted
    }
}

/// `refused_worktree_root(cwd)`: `(worktree_root, indeterminate)` read
/// from the files. `darwin` is `sys.platform == "darwin"`, which
/// `_spelled_as_git` reads. The Windows flavour declines whole: its
/// `realpath` is not ported, so `locate` there finds nothing where the
/// reference may find a repository.
pub fn refused_worktree_root<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    memo: &mut ConfigMemo,
    rule: &Rule<'_, F, A, R>,
    env: &Env,
    cwd: &[u8],
    darwin: bool,
) -> Result<(Option<Vec<u8>>, bool), RootFault> {
    let ctx = rule.ctx;
    if ctx.flavour == Flavour::Windows {
        return Err(RootFault::NotPorted);
    }
    let Some(found) = locate(rule, env, cwd, true, false) else {
        return Ok((None, false));
    };
    if config_moves_worktree(memo, ctx, &found.gd, rule.git)? {
        return Ok((None, true));
    }
    let spelled = match spelled_as_git(ctx.fs, &found.worktree_root, darwin) {
        Ok(Some(spelled)) => spelled,
        Ok(None) => return Ok((None, true)),
        Err(Refused::Raised(PyErr::Value)) => return Err(RootFault::Value),
        Err(_) => return Err(RootFault::NotPorted),
    };
    match realpath(ctx.fs, ctx.flavour, &spelled, ctx.cwd) {
        Ok(resolved) => Ok((Some(resolved), true)),
        Err(Unresolved::Raised(PyErr::Value)) => Err(RootFault::Value),
        Err(Unresolved::Raised(_)) => Ok((Some(spelled), true)),
        Err(Unresolved::NotPorted) => Err(RootFault::NotPorted),
    }
}

/// `_NOT_A_DIRECTORY`: the paths `first_not_a_directory` has seen, oldest
/// first, at most [`NOT_A_DIRECTORY_CAP`].
#[derive(Clone, Debug, Default)]
pub struct NotADirectorySeen {
    order: Vec<Vec<u8>>,
    seen: HashSet<Vec<u8>>,
}

impl NotADirectorySeen {
    pub fn new() -> NotADirectorySeen {
        NotADirectorySeen::default()
    }

    /// `_clear_not_a_directory`.
    pub fn clear(&mut self) {
        self.order.clear();
        self.seen.clear();
    }

    /// `first_not_a_directory(path)`, `path` spelled as `str(path)`:
    /// whether it is reported for the first time among the last
    /// [`NOT_A_DIRECTORY_CAP`] paths reported.
    pub fn first(&mut self, path: &[u8]) -> bool {
        if self.seen.contains(path) {
            return false;
        }
        self.seen.insert(path.to_vec());
        self.order.push(path.to_vec());
        if self.order.len() > NOT_A_DIRECTORY_CAP {
            let oldest = self.order.remove(0);
            self.seen.remove(&oldest);
        }
        true
    }
}
