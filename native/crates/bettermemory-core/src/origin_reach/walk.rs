//! Unit origin-3b of `origin.py`: the reachable walk's memo and
//! `commits_since_anchor` (see the README's "Repository roots, heads and
//! the reachable walk").
//!
//! - [`WalkMemo`]: `_WALK_MEMO` and `_WALK_TIMED_OUT` behind one lock
//!   (`_WALK_MEMO_LOCK`), with `_clear_walk_memo` and the table halves of
//!   `_timed_out_walk` and `_keep_timed_out_walk` (reference lines
//!   1278-1336); [`walk_memo`] is the process's one instance, registered
//!   with [`caches`] on first use;
//! - [`commits_since_anchor`] (1342-1484), over [`walk_reachable`];
//! - [`held_anchor`] (1487-1504): `_held_anchor`;
//! - [`WalkReads`]: [`Reads`] over [`RepositoryReads`] with the index memo
//!   and GIT_INDEX_FILE that `walk_files_signature` reads.
//!
//! The memo is a value the caller passes (the process's one, or a test's
//! own); the monotonic clock is [`Reads::monotonic`], read only where the
//! reference reads `time.monotonic()`.

use std::collections::{BTreeMap, HashMap};
use std::hash::{Hash, Hasher};
use std::sync::{Arc, LazyLock, Mutex, MutexGuard};

use super::{args, encode, repo_toplevel_and_head, walk_reachable, Error, Reads, RepositoryReads};
use crate::caches;
use crate::fsdata::realpath::Unresolved;
use crate::fsdata::{Flavour, Fs};
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::follow::Records;
use crate::githead::discovery::{Found, Owners};
use crate::githead::GitDir;
use crate::origin_dates::{is_full_commit_sha, OriginGit};
use crate::origin_git::OriginRunner;
use crate::origin_gitfiles::attrs::IndexMemo;
use crate::origin_gitfiles::facts::Held;
use crate::origin_gitfiles::walk::{walk_files_signature, WalkFiles};
use crate::origin_history::{
    unanswered, Cut, HistoryUnread, ReachableWalk, Unwalked, Walked, WalkedValue,
};
use crate::pyvalue::PyStr;
use crate::unicode::cps;

/// `_WALK_MEMO_CAP`: the most entries each of the two tables keeps.
pub const WALK_MEMO_CAP: usize = 128;
/// `_WALK_TIMED_OUT_SECONDS`: how long a timed-out walk is served.
pub const WALK_TIMED_OUT_SECONDS: f64 = 60.0;

/// `_WalkKey`: `(str(toplevel), anchor, head, walk_files_signature)`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WalkKey {
    pub root: PyStr,
    pub anchor: PyStr,
    pub head: PyStr,
    pub files: WalkFiles,
}

/// Hashed by its three texts: equal keys hold equal texts, and keys that
/// differ only in the files' signature share a hash, which `==` settles.
impl Hash for WalkKey {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.root.hash(state);
        self.anchor.hash(state);
        self.head.hash(state);
    }
}

/// What `_WALK_TIMED_OUT` keeps of a walk that met its timeout: the
/// commits listed alone, or why it was given up on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Kept {
    Walk(Arc<ReachableWalk>),
    Unread(HistoryUnread),
}

/// An `OrderedDict` keyed by [`WalkKey`]: each entry found by the key's
/// hash, as a dict finds it, with its place in the order (a running
/// number); `order` holds the keys by place, oldest first.
struct Ordered<V> {
    slots: HashMap<Arc<WalkKey>, (u64, V)>,
    order: BTreeMap<u64, Arc<WalkKey>>,
    next: u64,
}

impl<V> Default for Ordered<V> {
    fn default() -> Self {
        Ordered {
            slots: HashMap::new(),
            order: BTreeMap::new(),
            next: 0,
        }
    }
}

impl<V> Ordered<V> {
    fn get(&self, key: &WalkKey) -> Option<&V> {
        self.slots.get(key).map(|(_, value)| value)
    }

    /// `move_to_end(key)`, where `key` is held.
    fn move_to_end(&mut self, key: &WalkKey) {
        let Some((at, _)) = self.slots.get_mut(key) else {
            return;
        };
        if let Some(held) = self.order.remove(at) {
            *at = self.next;
            self.order.insert(self.next, held);
            self.next += 1;
        }
    }

    /// `self[key] = value`: a key already held keeps its place.
    fn set(&mut self, key: &WalkKey, value: V) {
        if let Some(slot) = self.slots.get_mut(key) {
            slot.1 = value;
            return;
        }
        let key = Arc::new(key.clone());
        self.order.insert(self.next, key.clone());
        self.slots.insert(key, (self.next, value));
        self.next += 1;
    }

    /// `del self[key]`, where `key` is held.
    fn remove(&mut self, key: &WalkKey) {
        if let Some((at, _)) = self.slots.remove(key) {
            self.order.remove(&at);
        }
    }

    /// `while len(self) > cap: self.popitem(last=False)`.
    fn evict(&mut self, cap: usize) {
        while self.slots.len() > cap {
            let Some((_, oldest)) = self.order.pop_first() else {
                break;
            };
            self.slots.remove(&*oldest);
        }
    }

    fn clear(&mut self) {
        self.slots.clear();
        self.order.clear();
    }

    /// The entries, oldest first.
    fn entries(&self) -> impl Iterator<Item = (&WalkKey, &V)> + '_ {
        self.order
            .values()
            .map(|key| (&**key, &self.slots[&**key].1))
    }
}

/// The two tables, each in the order its `OrderedDict` holds it.
#[derive(Default)]
struct Tables {
    memo: Ordered<Arc<ReachableWalk>>,
    timed_out: Ordered<(f64, Kept)>,
}

/// `_WALK_MEMO` and `_WALK_TIMED_OUT` behind `_WALK_MEMO_LOCK`: each
/// look-up-and-touch and each insert-and-evict is atomic.
pub struct WalkMemo {
    cap: usize,
    tables: Mutex<Tables>,
}

impl Default for WalkMemo {
    fn default() -> Self {
        Self::new()
    }
}

impl WalkMemo {
    /// A memo bounded at [`WALK_MEMO_CAP`].
    pub fn new() -> Self {
        Self::with_cap(WALK_MEMO_CAP)
    }

    /// A memo bounded at `cap` (the reference's cap patched, for tests).
    pub fn with_cap(cap: usize) -> Self {
        WalkMemo {
            cap,
            tables: Mutex::new(Tables::default()),
        }
    }

    fn lock(&self) -> MutexGuard<'_, Tables> {
        self.tables.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// `_clear_walk_memo`.
    pub fn clear(&self) {
        let mut t = self.lock();
        t.memo.clear();
        t.timed_out.clear();
    }

    /// `_WALK_MEMO.get(key)`, moved to the end where held.
    pub fn recall(&self, key: &WalkKey) -> Option<Arc<ReachableWalk>> {
        let mut t = self.lock();
        let walk = t.memo.get(key)?.clone();
        t.memo.move_to_end(key);
        Some(walk)
    }

    /// `_WALK_MEMO[key] = walk` (a key already held keeps its place),
    /// then the oldest entries evicted past the cap.
    pub fn store(&self, key: &WalkKey, walk: Arc<ReachableWalk>) {
        let mut t = self.lock();
        t.memo.set(key, walk);
        t.memo.evict(self.cap);
    }

    /// The table half of `_timed_out_walk`: what is kept for `key` less
    /// than [`WALK_TIMED_OUT_SECONDS`] ago by `now` (read only where an
    /// entry is held), moved to the end; an entry outside that window (a
    /// clock gone back included, and NaN) is removed. A clock that
    /// declines leaves the table as it was.
    pub fn recall_timed_out(
        &self,
        key: &WalkKey,
        now: &mut dyn FnMut() -> Result<f64, Error>,
    ) -> Result<Option<Kept>, Error> {
        let mut t = self.lock();
        let Some((stamp, kept)) = t.timed_out.get(key).cloned() else {
            return Ok(None);
        };
        if !(0.0..WALK_TIMED_OUT_SECONDS).contains(&(now()? - stamp)) {
            t.timed_out.remove(key);
            return Ok(None);
        }
        t.timed_out.move_to_end(key);
        Ok(Some(kept))
    }

    /// The table half of `_keep_timed_out_walk`: `key` set with `now`
    /// and moved to the end, then the oldest evicted past the cap.
    pub fn keep_timed_out(&self, key: &WalkKey, kept: Kept, now: f64) {
        let mut t = self.lock();
        t.timed_out.set(key, (now, kept));
        t.timed_out.move_to_end(key);
        t.timed_out.evict(self.cap);
    }

    /// The memo's keys, least recently used first.
    pub fn memo_keys(&self) -> Vec<WalkKey> {
        let t = self.lock();
        t.memo.entries().map(|(k, _)| k.clone()).collect()
    }

    /// The timed-out memo's entries (key, stamp, outcome), oldest first.
    pub fn timed_out_entries(&self) -> Vec<(WalkKey, f64, Kept)> {
        let t = self.lock();
        t.timed_out
            .entries()
            .map(|(k, (stamp, kept))| (k.clone(), *stamp, kept.clone()))
            .collect()
    }
}

/// The key `_clear_walk_memo` is registered under with [`caches`].
pub const CLEAR_KEY: &str = "origin._clear_walk_memo";

static WALK_MEMO: LazyLock<WalkMemo> = LazyLock::new(|| {
    caches::register(CLEAR_KEY, clear_walk_memo);
    WalkMemo::new()
});

/// The process's one memo, registered with [`caches`] on first use (an
/// unused memo is empty, so a clearing before then has nothing to clear).
pub fn walk_memo() -> &'static WalkMemo {
    &WALK_MEMO
}

/// `_clear_walk_memo`.
pub fn clear_walk_memo() {
    WALK_MEMO.clear();
}

/// `_git.timed_out_calls()`.
fn timeouts(runner: &OriginRunner<'_>) -> i64 {
    OriginGit::counts(runner).0
}

/// The root's bytes, or the error encoding it met. A key is made only
/// where the root has bytes, so every read after the key's holds them.
fn bytes(root: &Result<Vec<u8>, Error>) -> Result<&[u8], Error> {
    root.as_deref().map_err(|e| *e)
}

/// `_walk_reachable(toplevel, anchor, head, names=)` over the root's
/// bytes, or the error encoding it met: the anchor at the head is the
/// empty walk, answered before git runs, so only a walk that runs git
/// needs the bytes, and only it raises without them.
fn reach(
    runner: &mut OriginRunner<'_>,
    reads: &mut dyn Reads,
    root: &Result<Vec<u8>, Error>,
    anchor: &[u32],
    head: &[u32],
    names: bool,
) -> Result<Option<ReachableWalk>, Error> {
    match root {
        Ok(root) => walk_reachable(runner, reads, root, anchor, head, names),
        Err(_) if anchor == head => walk_reachable(runner, reads, &[], anchor, head, names),
        Err(e) => Err(*e),
    }
}

/// `_keep_timed_out_walk(key, kept, toplevel, held)`.
fn keep(
    memo: &WalkMemo,
    reads: &mut dyn Reads,
    key: Option<&WalkKey>,
    kept: Kept,
    root: &Result<Vec<u8>, Error>,
    held: &Held,
) -> Result<(), Error> {
    let Some(key) = key else { return Ok(()) };
    if reads.walk_files(bytes(root)?, None)?.as_ref() != Some(&key.files)
        || !reads.directories_held(held)?
    {
        return Ok(());
    }
    let now = reads.monotonic()?;
    memo.keep_timed_out(key, kept, now);
    Ok(())
}

/// A pass's `walked` entry served again, its failure or timeout counted
/// again on the runner.
fn replay(
    runner: &mut OriginRunner<'_>,
    unwalked: &mut Unwalked,
    seen: &WalkedValue,
) -> Option<Arc<ReachableWalk>> {
    match seen {
        WalkedValue::Walk(walk) => {
            if walk.timed_out {
                OriginGit::note_timed_out(runner);
            } else if !walk.listed {
                OriginGit::note_failed(runner);
            }
            return Some(walk.clone());
        }
        WalkedValue::Unread(why) => {
            if *why == HistoryUnread::Timeout {
                OriginGit::note_timed_out(runner);
            } else {
                OriginGit::note_failed(runner);
            }
            unwalked.note(*why);
        }
        WalkedValue::Failed(true) => OriginGit::note_failed(runner),
        WalkedValue::Failed(false) => {}
    }
    None
}

/// `commits_since_anchor(cwd, anchor, toplevel=, head=, walked=)`: the
/// reachable walk from `anchor` to the head, or None where the count
/// falls back to the author dates; `unwalked` takes the walks given up
/// on. `toplevel` is `str(toplevel)`; where it or `head` is None both are
/// read through [`repo_toplevel_and_head`].
#[allow(clippy::too_many_arguments)]
pub fn commits_since_anchor(
    memo: &WalkMemo,
    runner: &mut OriginRunner<'_>,
    reads: &mut dyn Reads,
    unwalked: &mut Unwalked,
    cwd: Option<&[u8]>,
    anchor: &[u32],
    located: (Option<&[u32]>, Option<&[u32]>),
    walked: Option<&mut Walked>,
) -> Result<Option<Arc<ReachableWalk>>, Error> {
    let Some(cwd) = cwd.filter(|_| is_full_commit_sha(anchor)) else {
        return Ok(None);
    };
    let (toplevel, head) = match located {
        (Some(toplevel), Some(head)) => (toplevel.to_vec(), head.to_vec()),
        _ => match repo_toplevel_and_head(runner, reads, Some(cwd))? {
            Some(pair) => pair,
            None => return Ok(None),
        },
    };
    // `str(toplevel)` as git and the reads take it. A root no file name
    // can hold (a lone surrogate on POSIX) has no bytes, and
    // `walk_files_signature` answers None for it, holding nothing: the
    // call is unkeyed and raises only where git would run there.
    let root = encode(reads, &toplevel);
    let mut held = Held::new();
    let files = match &root {
        Ok(root) => reads.walk_files(root, Some(&mut held))?,
        Err(_) => None,
    };
    let within = (toplevel.clone(), anchor.to_vec(), head.clone());
    let key = files.map(|files| WalkKey {
        root: toplevel,
        anchor: anchor.to_vec(),
        head: head.clone(),
        files,
    });
    if let Some(key) = &key {
        if let Some(stored) = memo.recall(key) {
            return Ok(Some(stored));
        }
        if let Some(kept) = memo.recall_timed_out(key, &mut || reads.monotonic())? {
            OriginGit::note_timed_out(runner);
            return Ok(match kept {
                Kept::Walk(walk) => Some(walk),
                Kept::Unread(why) => {
                    unwalked.note(why);
                    None
                }
            });
        }
    }
    if let Some(seen) = walked.as_deref().and_then(|w| w.get(&within)) {
        return Ok(replay(runner, unwalked, seen));
    }
    let (failed, timed_out) = (runner.failed_git_calls(), timeouts(runner));
    let mut walk = reach(runner, reads, &root, anchor, &head, true)?;
    if walk.is_none() && runner.failed_git_calls() != failed {
        // The names were not read: the commits listed alone.
        let names_timed_out = timeouts(runner) != timed_out;
        let (timed_out, listing) = (timeouts(runner), runner.failed_git_calls());
        walk = reach(runner, reads, &root, anchor, &head, false)?;
        let why = match walk {
            Some(_) => None,
            None if timeouts(runner) != timed_out => Some(HistoryUnread::Timeout),
            None if runner.failed_git_calls() != listing => {
                held_anchor(runner, bytes(&root)?, anchor)?
            }
            None => None,
        };
        if let Some(why) = why {
            unwalked.note(why);
            if names_timed_out || why == HistoryUnread::Timeout {
                keep(memo, reads, key.as_ref(), Kept::Unread(why), &root, &held)?;
            }
            if let Some(walked) = walked {
                walked.insert(within, WalkedValue::Unread(why));
            }
            return Ok(None);
        }
        if let Some(w) = walk.as_mut() {
            w.timed_out |= names_timed_out;
        }
    }
    let Some(walk) = walk.map(Arc::new) else {
        if let Some(walked) = walked {
            let moved = runner.failed_git_calls() != failed;
            walked.insert(within, WalkedValue::Failed(moved));
        }
        return Ok(None);
    };
    match key.as_ref().filter(|_| walk.listed) {
        None => {
            if walk.timed_out {
                keep(
                    memo,
                    reads,
                    key.as_ref(),
                    Kept::Walk(walk.clone()),
                    &root,
                    &held,
                )?;
            }
            if let Some(walked) = walked {
                walked.insert(within, WalkedValue::Walk(walk.clone()));
            }
        }
        Some(key) => {
            let now = reads.walk_files(bytes(&root)?, None)?;
            if now.as_ref() == Some(&key.files) && reads.directories_held(&held)? {
                memo.store(key, walk.clone());
            }
        }
    }
    Ok(Some(walk))
}

/// `_held_anchor(toplevel, anchor)`: ERROR where `git cat-file -e
/// <anchor>^{commit}` exits 0 or is killed by a signal, None where it
/// exits above 0, and `unanswered(before, named=True)` where it gives no
/// answer.
pub fn held_anchor(
    runner: &mut OriginRunner<'_>,
    toplevel: &[u8],
    anchor: &[u32],
) -> Result<Option<HistoryUnread>, Error> {
    let before = OriginGit::counts(runner);
    let mut ask = args(&["cat-file", "-e"]);
    ask.push([anchor, &cps("^{commit}")].concat());
    let Some(held) = runner.result(toplevel, &ask, 5.0, None, false)? else {
        return Ok(unanswered(before, OriginGit::counts(runner), true));
    };
    Ok((held.returncode <= 0).then_some(HistoryUnread::Error))
}

/// [`Reads`] over [`RepositoryReads`] with what `walk_files_signature`
/// reads beside them: the index memo (`_INDEX_MEMO`) and whether
/// GIT_INDEX_FILE is set. The root and head reads are the inner ones.
pub struct WalkReads<'a, 'r, F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized> {
    pub reads: &'a mut RepositoryReads<'r, F, A, R, O>,
    pub index_memo: &'a mut IndexMemo,
    pub index_file_set: bool,
}

impl<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized> Reads
    for WalkReads<'_, '_, F, A, R, O>
{
    fn flavour(&self) -> Flavour {
        self.reads.flavour()
    }
    fn find_gitdir(&mut self, cwd: &[u8]) -> Option<Found> {
        self.reads.find_gitdir(cwd)
    }
    fn head_sha(&mut self, gd: &GitDir) -> Option<String> {
        self.reads.head_sha(gd)
    }
    fn config_moves_worktree(&mut self, gd: &GitDir) -> Result<bool, Error> {
        self.reads.config_moves_worktree(gd)
    }
    fn machine_config_dies(&mut self, git_dir: &[u8]) -> Result<bool, Error> {
        self.reads.machine_config_dies(git_dir)
    }
    fn spelled_as_git(&mut self, root: &[u8]) -> Result<Option<Vec<u8>>, Error> {
        self.reads.spelled_as_git(root)
    }
    fn resolve(&mut self, path: &[u8]) -> Result<Vec<u8>, Unresolved> {
        self.reads.resolve(path)
    }
    fn cuts(&mut self, toplevel: &[u8]) -> Result<Option<Cut>, Error> {
        self.reads.cuts(toplevel)
    }
    fn walk_files(
        &mut self,
        root: &[u8],
        held: Option<&mut Held>,
    ) -> Result<Option<WalkFiles>, Error> {
        let inner = &mut *self.reads;
        walk_files_signature(
            inner.config_memo,
            self.index_memo,
            inner.rule,
            inner.host,
            inner.env,
            self.index_file_set,
            root,
            held,
        )
        .map_err(|_| Error::NotPorted)
    }
    fn directories_held(&mut self, held: &Held) -> Result<bool, Error> {
        self.reads.directories_held(held)
    }
    fn monotonic(&mut self) -> Result<f64, Error> {
        self.reads.monotonic()
    }
}
