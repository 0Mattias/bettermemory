//! The stamped reads that decide when a kept answer about a checkout's
//! remotes is reused: `location_git.py`'s `config_unchanged` (834-843),
//! `_STAMPED_READS`, `_FRESH_READS` and `_clear_stamped_reads` (846-870),
//! `_stamped_read` (873-894), `machine_config_stamps` (897-931),
//! `machine_config_reads` (934-959), `repository_config_stamps`
//! (962-986) and `_inside` (1002-1007).
//!
//! The reference keeps its reads in two process-wide dicts behind a lock;
//! here they are [`StampedReads`], a value the caller holds and passes in.
//! The clock (`time.monotonic`) is the caller's too: a `now` given to each
//! call that reads it, in seconds. The environment, the home directory,
//! what the git on PATH reads and the working directory are
//! [`Inputs`], as for the reads of [`super::remotes`].

use super::legacy::{file_stamp, Files, NotPorted, Stamp};
use super::paths::{self, Machine};
use super::remotes::{machine_configs, reads_worktree_config, Inputs, NotPortedWhy};
use super::tree::{config_tree, Ask, ConfigValues};
use crate::fsdata::{Flavour, Fs};
use std::collections::{BTreeMap, HashMap};

/// `_STAMPED_KEPT`: the most reads (and fresh marks) kept.
pub const STAMPED_KEPT: usize = 256;

/// `FRESH_SECONDS`.
pub const FRESH_SECONDS: f64 = 1.0;

/// `_STAMPED_READS`' key: the file, the git directory, the home directory
/// `~/` names.
pub type Key = (Vec<u8>, Vec<u8>, Option<Vec<u8>>);

/// An insertion-ordered map, as a Python dict is: a key set again keeps
/// its place unless it is taken out first; the oldest goes first.
#[derive(Clone, Debug, Default)]
struct Ordered<V> {
    next: u64,
    by_key: HashMap<Key, (u64, V)>,
    order: BTreeMap<u64, Key>,
}

impl<V> Ordered<V> {
    fn get(&self, key: &Key) -> Option<&(u64, V)> {
        self.by_key.get(key)
    }

    fn pop(&mut self, key: &Key) -> Option<V> {
        let (seq, v) = self.by_key.remove(key)?;
        self.order.remove(&seq);
        Some(v)
    }

    /// `d.pop(key, None); d[key] = v`, then the oldest dropped past `kept`.
    fn put_last(&mut self, key: Key, v: V, kept: usize) -> u64 {
        self.pop(&key);
        let seq = self.next;
        self.next += 1;
        self.order.insert(seq, key.clone());
        self.by_key.insert(key, (seq, v));
        while self.by_key.len() > kept {
            let Some((_, oldest)) = self.order.pop_first() else {
                break;
            };
            self.by_key.remove(&oldest);
        }
        seq
    }

    fn keys(&self) -> impl Iterator<Item = (&Key, u64)> {
        self.order.iter().map(|(seq, k)| (k, *seq))
    }

    fn len(&self) -> usize {
        self.by_key.len()
    }

    /// Every entry dropped; `next` kept.
    fn clear(&mut self) {
        self.by_key.clear();
        self.order.clear();
    }
}

/// `_STAMPED_READS` and `_FRESH_READS`, held by the caller.
#[derive(Clone, Debug, Default)]
pub struct StampedReads {
    reads: Ordered<ConfigValues>,
    fresh: Ordered<f64>,
}

impl StampedReads {
    pub fn new() -> StampedReads {
        StampedReads::default()
    }

    /// `_clear_stamped_reads`. The marks [`StampedReads::kept`] gives go on
    /// from where they were, so a read taken after a clear never carries
    /// the mark of one taken before it.
    pub fn clear(&mut self) {
        self.reads.clear();
        self.fresh.clear();
    }

    /// The reads kept, oldest first, each with the mark of the read it
    /// holds (a new mark each time a file is read again).
    pub fn kept(&self) -> Vec<(&Key, u64)> {
        self.reads.keys().collect()
    }

    /// The fresh marks, oldest first, with when each read was taken.
    pub fn fresh(&self) -> Vec<(&Key, f64)> {
        self.fresh
            .keys()
            .map(|(k, _)| (k, self.fresh.get(k).map_or(0.0, |e| e.1)))
            .collect()
    }

    /// How many reads are kept.
    pub fn len(&self) -> usize {
        self.reads.len()
    }

    pub fn is_empty(&self) -> bool {
        self.reads.len() == 0
    }
}

/// `gitconfig._home()`: HOME, and on Windows where it is unset or empty
/// the platform's.
fn home(machine: &Machine) -> Option<Vec<u8>> {
    match machine.flavour {
        Flavour::Posix => machine.home.clone(),
        Flavour::Windows => match &machine.home {
            Some(h) if !h.is_empty() => Some(h.clone()),
            _ => machine.profile.clone(),
        },
    }
}

/// A read's files with their stamps (`ConfigValues.files`).
pub fn files_of(read: &ConfigValues) -> Files {
    read.files
        .iter()
        .cloned()
        .zip(read.stamps.iter().copied())
        .collect()
}

/// `gitconfig.unchanged(files)`, a relative name stated from `cwd` as the
/// reference's `os.stat` resolves it: each path stamped once, compared in
/// order.
pub fn unchanged<F: Fs>(
    fs: &F,
    flavour: Flavour,
    cwd: &[u8],
    files: &[(Vec<u8>, Option<Stamp>)],
) -> Result<bool, NotPortedWhy> {
    let mut seen: HashMap<&[u8], Option<Stamp>> = HashMap::new();
    for (path, was) in files {
        let now = match seen.get(path.as_slice()) {
            Some(now) => *now,
            None => {
                let at = paths::opened(flavour, path, cwd);
                let now = file_stamp(fs, &at).map_err(|NotPorted| NotPortedWhy::Stamp)?;
                seen.insert(path, now);
                now
            }
        };
        if now != *was {
            return Ok(false);
        }
    }
    Ok(true)
}

/// `config_unchanged(cap)`: `sources` the capture's `config_sources`,
/// `files` its `config_files`.
pub fn config_unchanged<F: Fs>(
    fs: &F,
    inputs: &Inputs,
    sources: &[Vec<u8>],
    files: &[(Vec<u8>, Option<Stamp>)],
) -> Result<bool, NotPortedWhy> {
    if !sources.is_empty()
        && machine_configs(inputs.machine, &inputs.env, &inputs.sources) != sources
    {
        return Ok(false);
    }
    unchanged(fs, inputs.machine.flavour, &inputs.cwd, files)
}

/// `_stamped_read(path, git_dir)`: the kept read, unless a file it read
/// stamps otherwise, then read again and kept last.
pub fn stamped_read<F: Fs>(
    cache: &mut StampedReads,
    fs: &F,
    inputs: &Inputs,
    path: &[u8],
    git_dir: &[u8],
) -> Result<ConfigValues, NotPortedWhy> {
    let machine = inputs.machine;
    let key: Key = (path.to_vec(), git_dir.to_vec(), home(machine));
    if let Some((_, read)) = cache.reads.get(&key) {
        if unchanged(fs, machine.flavour, &inputs.cwd, &files_of(read))? {
            return Ok(read.clone());
        }
    }
    let ask = Ask {
        stamp_self: true,
        gentle: false,
        git_dir: Some(git_dir.to_vec()),
        cwd: inputs.cwd.clone(),
        context: None,
    };
    let read = config_tree(fs, machine, path, &ask);
    if read.raised.is_some() {
        return Err(NotPortedWhy::Raised);
    }
    if let Some(sub) = &read.not_ported {
        return Err(NotPortedWhy::Condition(sub.clone()));
    }
    if !read.stamps_held {
        return Err(NotPortedWhy::Stamp);
    }
    cache.reads.put_last(key, read.clone(), STAMPED_KEPT);
    Ok(read)
}

/// What `machine_config_stamps` answers: the files named, then each
/// one's read's files with their stamps.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MachineStamps {
    pub named: Vec<Vec<u8>>,
    pub files: Vec<Files>,
}

/// `machine_config_stamps(git_dir, reuse=...)`, `now` the clock's
/// reading at the call.
pub fn machine_config_stamps<F: Fs>(
    cache: &mut StampedReads,
    fs: &F,
    inputs: &Inputs,
    git_dir: &[u8],
    reuse: bool,
    now: f64,
) -> Result<MachineStamps, NotPortedWhy> {
    let named = machine_configs(inputs.machine, &inputs.env, &inputs.sources);
    let home = home(inputs.machine);
    let mut files = Vec::with_capacity(named.len());
    for path in &named {
        let key: Key = (path.clone(), git_dir.to_vec(), home.clone());
        let taken = cache.fresh.pop(&key);
        let kept = cache.reads.get(&key).map(|(_, r)| files_of(r));
        let fresh = taken.is_some_and(|t| (0.0..FRESH_SECONDS).contains(&(now - t)));
        let got = match kept {
            Some(kept) if reuse && fresh => kept,
            _ => files_of(&stamped_read(cache, fs, inputs, path, git_dir)?),
        };
        files.push(got);
    }
    Ok(MachineStamps { named, files })
}

/// `machine_config_reads(git_dir)`: `clock` is read once before each
/// file no capture has read yet.
pub fn machine_config_reads<F: Fs>(
    cache: &mut StampedReads,
    fs: &F,
    inputs: &Inputs,
    git_dir: &[u8],
    clock: &mut dyn FnMut() -> f64,
) -> Result<Vec<ConfigValues>, NotPortedWhy> {
    let home = home(inputs.machine);
    let mut out = Vec::new();
    for path in machine_configs(inputs.machine, &inputs.env, &inputs.sources) {
        let key: Key = (path.clone(), git_dir.to_vec(), home.clone());
        let read = match cache.reads.get(&key) {
            Some((_, read)) => read.clone(),
            None => {
                let started = clock();
                let read = stamped_read(cache, fs, inputs, &path, git_dir)?;
                cache.fresh.put_last(key, started, STAMPED_KEPT);
                read
            }
        };
        out.push(read);
    }
    Ok(out)
}

/// `repository_config_stamps(common_dir, git_dir)`.
pub fn repository_config_stamps<F: Fs>(
    cache: &mut StampedReads,
    fs: &F,
    inputs: &Inputs,
    common_dir: &[u8],
    git_dir: &[u8],
) -> Result<Vec<Files>, NotPortedWhy> {
    let flavour = inputs.machine.flavour;
    let common = paths::join(flavour, common_dir, b"config");
    let mut out = vec![files_of(&stamped_read(
        cache, fs, inputs, &common, git_dir,
    )?)];
    if reads_worktree_config(fs, flavour, common_dir, &inputs.git) == Some(true) {
        let worktree = paths::join(flavour, git_dir, b"config.worktree");
        out.push(files_of(&stamped_read(
            cache, fs, inputs, &worktree, git_dir,
        )?));
    }
    Ok(out)
}

/// `posixpath.normpath`.
fn normpath(path: &[u8]) -> Vec<u8> {
    if path.is_empty() {
        return b".".to_vec();
    }
    let lead = if path.starts_with(b"//") && !path.starts_with(b"///") {
        2
    } else if path.starts_with(b"/") {
        1
    } else {
        0
    };
    let mut parts: Vec<&[u8]> = Vec::new();
    for part in path.split(|&b| b == b'/') {
        if part.is_empty() || part == b"." {
            continue;
        }
        if part != b".." || (lead == 0 && parts.last().is_none_or(|p| *p == b"..")) {
            parts.push(part);
        } else if !parts.is_empty() {
            parts.pop();
        }
    }
    let mut out = vec![b'/'; lead];
    out.extend_from_slice(&parts.join(&b'/'));
    if out.is_empty() {
        out.push(b'.');
    }
    out
}

/// `_inside(path, root)` on POSIX: both normalized, then the same, or
/// `path` under `root` and a separator. None under the Windows flavour,
/// whose `ntpath.normpath` is not ported.
pub fn inside(flavour: Flavour, path: &[u8], root: &[u8]) -> Option<bool> {
    if flavour == Flavour::Windows {
        return None;
    }
    let (path, root) = (normpath(path), normpath(root));
    if path == root {
        return Some(true);
    }
    let mut prefix = root;
    if !prefix.ends_with(b"/") {
        prefix.push(b'/');
    }
    Some(path.starts_with(&prefix))
}
