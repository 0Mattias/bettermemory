//! The readers of `location_git.py` that need neither git's discovery nor
//! the object reader: the repository record (`_Git`), the head
//! (`_resolve_head`), the ref tips (`_ref_tips`), the content manifest
//! (`_manifest`) and the bounded history walks (`Reach`,
//! `reachable_among`, `walk`).
//!
//! The rest of the reference module lives elsewhere: lines 259-1001 (the
//! remotes, their spellings and the stamps) in `gitconfig::{remotes,
//! spellings, stamped}`, `_inside` in `gitconfig::stamped::inside`, the
//! capture's vocabulary (1188-1451) and the kinds (103-110) in
//! [`crate::capture`], discovery (`_discover`, `_at_dotgit`, `_from_env`)
//! in [`crate::location_discovery`]. `_git`, `RepositoryHistory` and
//! `_clear_above` are not ported yet.
//!
//! Effects are arguments: the head goes through githead's [`RefFs`], the
//! walks through [`Fs`], the working directory through [`Process`], a
//! tag's peel and the history through closures. See native/README.md,
//! "The readers of a repository's head, refs, tree and history".

use std::collections::{BTreeSet, HashMap, VecDeque};

use crate::capture::paths::basename;
use crate::capture::{Kind, Manifest, MANIFEST_LIMIT, REF_TIPS_MAX};
use crate::fsdata::{Flavour, Fs, Kind as FileKind, PyErr};
use crate::gitconfig::paths::code_points;
use crate::gitfile_rule::hostpath::text::{relpath, Process, Raised};
use crate::githead::{full_hash, head_commit, head_ref, GitDir, HeadCommit, RefFs};

/// `WALK_LIMIT`: the most commits a walk reads.
pub const WALK_LIMIT: usize = 512;
/// `EDGE_LIMIT`: the parent edges `reachable_among` reads and spreads.
pub const EDGE_LIMIT: u64 = 1 << 18;
/// `_PROJECT_FILE`: the root's project file, left out of the manifest.
pub const PROJECT_FILE: &[u8] = b"bettermemory.toml";

/// `str.split()` and `str.strip()` over ASCII-decoded text: the ten
/// ASCII whitespace bytes (U+FFFD, a non-ASCII byte's, is not one).
fn space(b: u8) -> bool {
    matches!(b, b'\t'..=b'\r' | 0x1c..=0x1f | b' ')
}

fn strip(text: &[u8]) -> &[u8] {
    let start = text.iter().position(|&b| !space(b)).unwrap_or(text.len());
    let end = text
        .iter()
        .rposition(|&b| !space(b))
        .map_or(start, |i| i + 1);
    &text[start..end]
}

/// `os.path.join(base, name)` (posixpath): a separator unless `base` is
/// empty or ends with one.
fn join(base: &[u8], name: &[u8]) -> Vec<u8> {
    let mut out = base.to_vec();
    if !out.is_empty() && out.last() != Some(&b'/') {
        out.push(b'/');
    }
    out.extend_from_slice(name);
    out
}

/// `_Git`: a repository found from a directory, its paths as bytes
/// (`os.fsencode` of the reference's strings).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Git {
    pub dotgit: Option<Vec<u8>>,
    pub git_dir: Vec<u8>,
    pub common_dir: Vec<u8>,
    pub root: Option<Vec<u8>>,
    pub kind: Kind,
}

/// `_resolve_head`: (the commit HEAD names, lowercased; the branch). A
/// HEAD that ends at a ref outside `refs/heads/` is peeled by `peel`,
/// given the ref's hash (`githead.head_commit`'s object read).
pub fn resolve_head(
    fs: &dyn RefFs,
    git: &Git,
    peel: &mut dyn FnMut(&str) -> Option<String>,
) -> (Option<String>, Option<String>) {
    let gd = GitDir {
        gitdir: git.git_dir.clone(),
        commondir: git.common_dir.clone(),
    };
    let sha = match head_commit(fs, &gd) {
        HeadCommit::Answer(sha) => Some(sha),
        HeadCommit::Peel(id) => peel(&id),
        HeadCommit::Decline => None,
    };
    let branch = head_ref(fs, &gd).and_then(|r| r.strip_prefix("refs/heads/").map(str::to_owned));
    (sha.map(|s| s.to_lowercase()), branch)
}

/// `str` order of names as `os.fsdecode` gives them.
fn sorted_names(mut names: Vec<Vec<u8>>) -> Vec<Vec<u8>> {
    names.sort_by_cached_key(|n| code_points(n));
    names
}

fn read_up_to<F: Fs + ?Sized>(fs: &F, path: &[u8], max: usize) -> Result<Vec<u8>, PyErr> {
    let mut file = fs.open(path)?;
    let mut out = Vec::new();
    while out.len() < max {
        let chunk = fs.read(&mut file, (max - out.len()).min(1 << 16))?;
        if chunk.is_empty() {
            break;
        }
        out.extend_from_slice(&chunk);
    }
    Ok(out)
}

/// `os.walk(top)` top down, `dirs.sort()` and files sorted: each
/// directory's files, in the order the reference reads them; `stop` ends
/// the walk early.
fn walk_files<F: Fs + ?Sized>(fs: &F, top: &[u8], visit: &mut dyn FnMut(&[u8]) -> bool) -> bool {
    let mut stack = vec![top.to_vec()];
    while let Some(directory) = stack.pop() {
        let Ok(names) = fs.listdir(&directory) else {
            continue;
        };
        let (mut dirs, mut files) = (Vec::new(), Vec::new());
        for name in names {
            let path = join(&directory, &name);
            let is_dir = fs.stat(&path).is_ok_and(|s| s.kind == FileKind::Directory);
            if is_dir {
                dirs.push(name)
            } else {
                files.push(name)
            }
        }
        for name in sorted_names(files) {
            if visit(&join(&directory, &name)) {
                return true;
            }
        }
        for name in sorted_names(dirs).iter().rev() {
            let path = join(&directory, name);
            let link = fs
                .lstat_result(&path)
                .is_ok_and(|s| s.kind == FileKind::Link);
            if !link {
                stack.push(path);
            }
        }
    }
    false
}

/// `_ref_tips`: up to [`REF_TIPS_MAX`] commit ids the branch and
/// remote-tracking refs hold (loose refs, then packed-refs), sorted.
pub fn ref_tips<F: Fs + ?Sized>(fs: &F, common_dir: &[u8]) -> Vec<String> {
    let mut tips: BTreeSet<Vec<u8>> = BTreeSet::new();
    let done = |tips: &BTreeSet<Vec<u8>>| {
        tips.iter()
            .map(|t| String::from_utf8_lossy(t).into_owned())
            .collect::<Vec<_>>()
    };
    for sub in [&b"refs/heads"[..], b"refs/remotes"] {
        let top = join(common_dir, sub);
        let full = walk_files(fs, &top, &mut |path| {
            let Ok(data) = read_up_to(fs, path, 128) else {
                return false;
            };
            let value = strip(&data);
            if full_hash(value) {
                tips.insert(value.to_vec());
            }
            tips.len() >= REF_TIPS_MAX
        });
        if full {
            return done(&tips);
        }
    }
    // `for raw in fh`: lines are read as they come and the read stops at
    // the last tip it needs; a read error keeps the tips taken before it.
    let take = |raw: &[u8], tips: &mut BTreeSet<Vec<u8>>| {
        let mut parts = raw.split(|&b| space(b)).filter(|p| !p.is_empty());
        if let (Some(sha), Some(name), None) = (parts.next(), parts.next(), parts.next()) {
            if (name.starts_with(b"refs/heads/") || name.starts_with(b"refs/remotes/"))
                && full_hash(sha)
            {
                tips.insert(sha.to_vec());
            }
        }
        tips.len() >= REF_TIPS_MAX
    };
    if let Ok(mut file) = fs.open(&join(common_dir, b"packed-refs")) {
        let mut pending: Vec<u8> = Vec::new();
        loop {
            let Ok(chunk) = fs.read(&mut file, 1 << 16) else {
                break;
            };
            if chunk.is_empty() {
                if !pending.is_empty() {
                    take(&pending, &mut tips);
                }
                break;
            }
            pending.extend_from_slice(&chunk);
            let mut start = 0;
            let mut full = false;
            while let Some(n) = pending[start..].iter().position(|&b| b == b'\n') {
                full = take(&pending[start..=start + n], &mut tips);
                start += n + 1;
                if full {
                    break;
                }
            }
            if full {
                break;
            }
            pending.drain(..start);
        }
    }
    done(&tips)
}

/// `_manifest(root)`: the folder's name and the (path, size) of up to
/// [`MANIFEST_LIMIT`] entries outside `.git` and the root's project file,
/// sorted. Raises what `os.path.relpath` raises (the working directory,
/// asked where `root` is relative).
pub fn manifest<F: Fs + ?Sized>(
    fs: &F,
    flavour: Flavour,
    process: &dyn Process,
    root: &[u8],
) -> Result<Manifest, Raised> {
    let root_text = code_points(root);
    let sep = crate::gitfile_rule::hostpath::text::sep(flavour);
    let mut entries: Vec<(Vec<u32>, u64)> = Vec::new();
    let mut todo = vec![root.to_vec()];
    while entries.len() < MANIFEST_LIMIT {
        let Some(directory) = todo.pop() else { break };
        let Ok(names) = fs.listdir(&directory) else {
            continue;
        };
        let mut subdirs = Vec::new();
        for name in sorted_names(names) {
            if name == b".git" {
                continue;
            }
            let path = join(&directory, &name);
            let Ok(st) = fs.lstat_result(&path) else {
                continue;
            };
            if st.kind == FileKind::Directory {
                subdirs.push(path);
                continue;
            }
            if directory == root && name == PROJECT_FILE {
                continue;
            }
            let rel = relpath(flavour, &code_points(&path), &root_text, process)?;
            let rel = rel
                .iter()
                .map(|&c| if c == sep { u32::from(b'/') } else { c })
                .collect();
            entries.push((rel, st.size));
            if entries.len() >= MANIFEST_LIMIT {
                break;
            }
        }
        todo.extend(subdirs.into_iter().rev());
    }
    entries.sort();
    Ok(Manifest {
        name: basename(&root_text),
        entries: entries
            .into_iter()
            .map(|(p, s)| (p, s.to_string()))
            .collect(),
    })
}

/// A Python integer used as a set of given commits: one bit each.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct Mask(Vec<u64>);

impl Mask {
    fn bit(i: usize) -> Mask {
        let mut words = vec![0; i / 64 + 1];
        words[i / 64] = 1 << (i % 64);
        Mask(words)
    }
    fn trimmed(mut self) -> Mask {
        while self.0.last() == Some(&0) {
            self.0.pop();
        }
        self
    }
    fn or(&self, other: &Mask) -> Mask {
        let n = self.0.len().max(other.0.len());
        let at = |m: &Mask, i: usize| m.0.get(i).copied().unwrap_or(0);
        Mask((0..n).map(|i| at(self, i) | at(other, i)).collect()).trimmed()
    }
    fn has(&self, i: usize) -> bool {
        self.0.get(i / 64).is_some_and(|w| w >> (i % 64) & 1 == 1)
    }
}

/// `Reach`: {commit: the given commits it is reachable from}, in the
/// given commits' sorted order, each set sorted; `bounded` and `handled`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Reach {
    pub relations: Vec<(String, Vec<String>)>,
    pub bounded: bool,
    pub handled: u64,
}

/// A dict used as an ordered set (`held`): insertion order, deletion.
#[derive(Default)]
struct Ordered {
    slots: Vec<Option<String>>,
    at: HashMap<String, usize>,
}

impl Ordered {
    fn insert(&mut self, sha: &str) {
        if !self.at.contains_key(sha) {
            self.at.insert(sha.to_owned(), self.slots.len());
            self.slots.push(Some(sha.to_owned()));
        }
    }
    fn remove(&mut self, sha: &str) {
        if let Some(i) = self.at.remove(sha) {
            self.slots[i] = None;
        }
    }
    fn items(&self) -> Vec<String> {
        self.slots.iter().flatten().cloned().collect()
    }
    fn take(&mut self) -> Vec<String> {
        let out = self.items();
        *self = Ordered::default();
        out
    }
}

struct Walk<'a> {
    parents: &'a mut dyn FnMut(&str) -> Vec<String>,
    committed: Option<&'a mut dyn FnMut(&str) -> i128>,
    limit: usize,
    edges: u64,
    lines: HashMap<String, Vec<String>>,
    times: HashMap<String, i128>,
    child: HashMap<String, i128>,
    held: Ordered,
    todo: VecDeque<String>,
    queued: std::collections::HashSet<String>,
    handled: u64,
    rise: u128,
    floor: i128,
}

impl Walk<'_> {
    fn read(&mut self, sha: &str) -> bool {
        if !self.lines.contains_key(sha) {
            if self.lines.len() >= self.limit || self.handled >= self.edges {
                return false;
            }
            let line: Vec<String> = (self.parents)(sha)
                .iter()
                .map(|p| p.to_lowercase())
                .collect();
            let when = self.committed.as_mut().map_or(0, |c| c(sha));
            self.handled += line.len() as u64;
            self.lines.insert(sha.to_owned(), line);
            self.times.insert(sha.to_owned(), when);
        }
        true
    }

    fn lower(&mut self, by: u128) {
        self.rise = by;
        for sha in self.held.items() {
            if at_least(self.times[&sha], self.floor, self.rise) {
                self.held.remove(&sha);
                if self.queued.insert(sha.clone()) {
                    self.todo.push_back(sha);
                }
            }
        }
    }

    fn reached(&mut self, parent: &str, when: i128) {
        if self.child.get(parent).is_none_or(|&c| when < c) {
            self.child.insert(parent.to_owned(), when);
            if let Some(&t) = self.times.get(parent) {
                if let Some(d) = rise_over(t, when, self.rise) {
                    self.lower(d);
                }
            }
        }
    }
}

/// `x >= floor - rise` over Python's unbounded integers: `floor - rise`
/// may fall below `i128::MIN`, so the difference is never formed.
fn at_least(x: i128, floor: i128, rise: u128) -> bool {
    x >= floor || floor.abs_diff(x) <= rise
}

/// `a - b` when it exceeds `rise` (Python's unbounded `a - b > rise`):
/// the gap of two `i128` values fits a `u128`, so nothing overflows.
fn rise_over(a: i128, b: i128, rise: u128) -> Option<u128> {
    (a > b && a.abs_diff(b) > rise).then(|| a.abs_diff(b))
}

/// `reachable_among(history, commits, limit=, edges=, committed=)`: the
/// history as closures (a commit's parents; its committer time, every
/// commit 0 without it).
pub fn reachable_among<'a>(
    parents: &'a mut dyn FnMut(&str) -> Vec<String>,
    commits: &[String],
    limit: usize,
    edges: u64,
    committed: Option<&'a mut dyn FnMut(&str) -> i128>,
) -> Reach {
    let given: Vec<String> = commits
        .iter()
        .map(|c| c.to_lowercase())
        .filter(|c| full_hash(c.as_bytes()))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let mut reach: HashMap<String, Mask> = given
        .iter()
        .enumerate()
        .map(|(i, c)| (c.clone(), Mask::bit(i)))
        .collect();
    let mut spread: HashMap<String, Mask> = HashMap::new();
    let mut w = Walk {
        parents,
        committed,
        limit,
        edges,
        lines: HashMap::new(),
        times: HashMap::new(),
        child: HashMap::new(),
        held: Ordered::default(),
        todo: given.iter().cloned().collect(),
        queued: given.iter().cloned().collect(),
        handled: 0,
        rise: 0,
        floor: 0,
    };
    let mut floor: Option<i128> = None;
    for c in &given {
        if w.read(c) {
            let t = w.times[c];
            floor = Some(floor.map_or(t, |f: i128| f.min(t)));
        }
    }
    w.floor = floor.unwrap_or(0);
    let (mut bounded, mut cut, mut deep) = (false, false, false);
    loop {
        while let Some(sha) = w.todo.pop_front() {
            w.queued.remove(&sha);
            if !w.read(&sha) {
                bounded = bounded || !deep;
                cut = true;
                continue;
            }
            let when = w.times[&sha];
            if let Some(&c) = w.child.get(&sha) {
                if let Some(d) = rise_over(when, c, w.rise) {
                    w.lower(d);
                }
            }
            let line = w.lines[&sha].clone();
            if !deep && !at_least(when, w.floor, w.rise) {
                w.held.insert(&sha);
                for p in &line {
                    w.reached(p, when);
                }
                continue;
            }
            let mask = reach.get(&sha).cloned().unwrap_or_default();
            if spread.get(&sha) == Some(&mask) {
                continue;
            }
            if w.handled + line.len() as u64 > w.edges {
                bounded = bounded || !deep;
                cut = true;
                continue;
            }
            w.handled += line.len() as u64;
            spread.insert(sha.clone(), mask.clone());
            for p in &line {
                w.reached(p, when);
                let old = reach.get(p).cloned().unwrap_or_default();
                let new = old.or(&mask);
                if new != old {
                    reach.insert(p.clone(), new);
                    if w.queued.insert(p.clone()) {
                        w.todo.push_back(p.clone());
                    }
                }
            }
        }
        let held = w.held.items();
        if deep || held.is_empty() {
            break;
        }
        deep = true;
        for sha in w.held.take() {
            w.todo.push_back(sha.clone());
            w.queued.insert(sha);
        }
    }
    let relations = given
        .iter()
        .map(|c| {
            let mask = &reach[c];
            let from = given
                .iter()
                .enumerate()
                .filter(|(i, o)| *o != c && mask.has(*i))
                .map(|(_, o)| o.clone())
                .collect();
            (c.clone(), from)
        })
        .collect();
    Reach {
        relations,
        bounded: bounded || (cut && w.rise > 0),
        handled: w.handled,
    }
}

/// One commit `walk` read: (fingerprint, parents).
pub type Walked = (String, (Option<String>, Vec<String>));

/// `walk(history, head, ref_tips)`: from HEAD, or the ref tips when HEAD
/// is unborn (None or empty), by a stack, up to [`WALK_LIMIT`] commits, in
/// the order read.
pub fn walk(
    parents: &mut dyn FnMut(&str) -> Vec<String>,
    fingerprint: &mut dyn FnMut(&str) -> Option<String>,
    head: Option<&str>,
    ref_tips: &[String],
) -> Vec<Walked> {
    let mut out: Vec<Walked> = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let mut todo: Vec<String> = match head {
        Some(h) if !h.is_empty() => vec![h.to_owned()],
        _ => ref_tips.to_vec(),
    };
    while out.len() < WALK_LIMIT {
        let Some(sha) = todo.pop() else { break };
        if !seen.insert(sha.clone()) {
            continue;
        }
        let line = parents(&sha);
        out.push((sha.clone(), (fingerprint(&sha), line.clone())));
        todo.extend(line);
    }
    out
}
