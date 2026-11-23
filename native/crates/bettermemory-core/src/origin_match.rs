//! Caller-side repository spellings and the legacy rc3 worktree comparison in origin.py.
use crate::{
    claims::strip,
    fsdata::{Numbered, PyErr},
    gitfile_rule::probe::path_gone,
    origin_remote::{parse_remote, Triple},
    pyre::{regex::Regex, tree::MULTILINE},
    pyvalue::PyStr,
    records::Origin,
    unicode::{cps, lower},
};
use std::{
    collections::VecDeque,
    sync::{Arc, LazyLock, Mutex},
};

const ALTERNATES_CAP: usize = 32;
const ROOTS_CAP: usize = 64;

type PrimaryEntry = (PyStr, Option<Arc<[u32]>>);

/// The DEBUG log format in origin._worktree_root_is_gone. Arguments are the original
/// path and exception; matching continues only after the logger returns.
pub const INDETERMINATE_ROOT: &str = "cannot determine whether worktree root %s still exists (%s); keeping worktree isolation in force";

/// A semantic error raised while interpreting a worktree marker.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MatchError {
    Regex(crate::pyre::regex::CallError),
    Value(&'static str),
}

/// Filesystem and logging operations called by the rc3 worktree comparison.
/// An unreadable or absent gitfile gives None; an OSError from Path.resolve gives
/// None so the original captured prefix can be returned. Other errors propagate.
pub trait WorktreeReads {
    type Error: From<MatchError>;

    fn gitfile(&mut self, worktree: &[u32]) -> Result<Option<Vec<u8>>, Self::Error>;
    fn resolve(&mut self, prefix: &[u32]) -> Result<Option<PyStr>, Self::Error>;
    fn stat(&mut self, worktree: &[u32]) -> Result<(), Numbered>;
    fn indeterminate(&mut self, worktree: &[u32], error: Numbered) -> Result<(), Self::Error>;
}

/// One process's 32 insertion-ordered alternate sets and its separate 64-entry
/// least-recently-used primary-root memo. Capture and lookup borrow the same owner.
#[derive(Default)]
pub struct OriginMatcher {
    alternates: Mutex<VecDeque<(PyStr, Arc<[PyStr]>)>>,
    primary_roots: Mutex<VecDeque<PrimaryEntry>>,
}

static PROCESS: LazyLock<OriginMatcher> = LazyLock::new(OriginMatcher::default);

/// Shared by the production capture, recall and health callers.
pub fn process_matcher() -> &'static OriginMatcher {
    &PROCESS
}

fn lower_triple((host, owner, name): Triple) -> Triple {
    (lower(&host), lower(&owner), lower(&name))
}

fn spelling_matches(memory: &[u32], normalized: &Option<Triple>, current: &[u32]) -> bool {
    match (normalized, parse_remote(current)) {
        (Some(a), Some(b)) => *a == lower_triple(b),
        _ => memory == current,
    }
}

impl OriginMatcher {
    pub fn new() -> Self {
        Self::default()
    }

    /// Replace even with an empty tuple, without moving a refreshed entry.
    pub fn register_caller_alternates(&self, repo: PyStr, alternates: Vec<PyStr>) {
        let values = Arc::from(alternates);
        let mut entries = self.alternates.lock().unwrap();
        if let Some((_, old)) = entries.iter_mut().find(|(key, _)| *key == repo) {
            *old = values;
            return;
        }
        if entries.len() == ALTERNATES_CAP {
            entries.pop_front();
        }
        entries.push_back((repo, values));
    }

    /// Match normalized triples, falling back to exact text when either URL is opaque.
    pub fn repos_match(
        &self,
        memory: Option<&[u32]>,
        current: Option<&[u32]>,
        explicit: Option<&[PyStr]>,
    ) -> bool {
        let (Some(memory), Some(current)) = (memory, current) else {
            return true;
        };
        let normalized = parse_remote(memory).map(lower_triple);
        if spelling_matches(memory, &normalized, current) {
            return true;
        }
        let snapshot;
        let alternatives = match explicit {
            Some(values) => values,
            None => {
                snapshot = self
                    .alternates
                    .lock()
                    .unwrap()
                    .iter()
                    .find(|(name, _)| name == current)
                    .map(|(_, values)| Arc::clone(values));
                snapshot.as_deref().unwrap_or_default()
            }
        };
        alternatives
            .iter()
            .any(|alt| alt != current && spelling_matches(memory, &normalized, alt))
    }

    /// origin._primary_root_of. Cache misses run outside the lock, just as in
    /// functools.lru_cache. Overlapping misses each return their own answer,
    /// while the first completed result remains cached.
    pub fn primary_root_of<R: WorktreeReads>(
        &self,
        worktree: &[u32],
        reads: &mut R,
    ) -> Result<Option<Arc<[u32]>>, R::Error> {
        {
            let mut entries = self.primary_roots.lock().unwrap();
            if let Some(index) = entries.iter().position(|(name, _)| name == worktree) {
                let entry = entries.remove(index).unwrap();
                let value = entry.1.clone();
                entries.push_back(entry);
                return Ok(value);
            }
        }
        let value = match reads.gitfile(worktree)? {
            Some(bytes) => match primary_prefix(&bytes)? {
                Some(prefix) => Some(Arc::from(reads.resolve(&prefix)?.unwrap_or(prefix))),
                None => None,
            },
            None => None,
        };
        let mut entries = self.primary_roots.lock().unwrap();
        if !entries.iter().any(|(name, _)| name == worktree) {
            if entries.len() == ROOTS_CAP {
                entries.pop_front();
            }
            entries.push_back((worktree.to_vec(), value.clone()));
        }
        Ok(value)
    }

    /// rc3 worktree comparison. Only the caller's linked checkout is resolved.
    pub fn worktrees_match<R: WorktreeReads>(
        &self,
        memory: Option<&[u32]>,
        caller: Option<&[u32]>,
        reads: &mut R,
    ) -> Result<bool, R::Error> {
        let (Some(memory), Some(caller)) = (memory, caller) else {
            return Ok(true);
        };
        if memory == caller || self.primary_root_of(caller, reads)?.as_deref() == Some(memory) {
            return Ok(true);
        }
        match reads.stat(memory) {
            Ok(()) => Ok(false),
            Err(e) if e.error.class == PyErr::Value || path_gone(e.error.into()) => Ok(true),
            Err(e) => {
                reads.indeterminate(memory, e)?;
                Ok(false)
            }
        }
    }

    /// The historical rc3 shadow value. The membership-record admission has its own owner.
    pub fn should_include_for_caller<R: WorktreeReads>(
        &self,
        origin: Option<&Origin>,
        current_repo: Option<&[u32]>,
        caller_root: Option<&[u32]>,
        explicit: Option<&[PyStr]>,
        reads: &mut R,
    ) -> Result<bool, R::Error> {
        if !self.repos_match(
            origin.and_then(|o| o.repo.as_deref()),
            current_repo,
            explicit,
        ) {
            return Ok(false);
        }
        self.worktrees_match(
            origin.and_then(|o| o.worktree_root.as_deref()),
            caller_root,
            reads,
        )
    }
}

static GITDIR: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new_str(r"^gitdir:\s*(.+)$", MULTILINE).expect("supported origin.py gitfile pattern")
});

fn primary_prefix(raw: &[u8]) -> Result<Option<PyStr>, MatchError> {
    let decoded = String::from_utf8_lossy(raw);
    let text = cps(&decoded.replace("\r\n", "\n").replace('\r', "\n"));
    let Some(hit) = GITDIR
        .search(text.as_slice(), 0, isize::MAX)
        .map_err(MatchError::Regex)?
    else {
        return Ok(None);
    };
    let (begin, end) = hit.group(1).expect("required gitdir capture");
    let path = strip(&text[begin as usize..end as usize]);
    for marker in ["/.git/worktrees/", r"\.git\worktrees\"] {
        let needle = cps(marker);
        if let Some(i) = path.windows(needle.len()).position(|v| v == needle) {
            return Ok(Some(path[..i].to_vec()));
        }
    }
    Ok(None)
}

/// The concrete POSIX filesystem used by the production caller. A configured DEBUG
/// sink receives the original worktree and typed OS error; without one DEBUG is off.
#[cfg(unix)]
pub struct NativeWorktrees<L = fn(&[u32], Numbered) -> Result<(), MatchError>> {
    pub debug: L,
}

#[cfg(unix)]
impl Default for NativeWorktrees {
    fn default() -> Self {
        Self {
            debug: |_, _| Ok(()),
        }
    }
}

#[cfg(unix)]
impl<L, E> WorktreeReads for NativeWorktrees<L>
where
    L: FnMut(&[u32], Numbered) -> Result<(), E>,
    E: From<MatchError>,
{
    type Error = E;

    fn gitfile(&mut self, worktree: &[u32]) -> Result<Option<Vec<u8>>, E> {
        use crate::fsdata::{Fs, Kind, StdFs};
        use crate::gitfile_rule::follow::fsencode;
        use crate::githead::discovery::pure;
        let Some(bytes) = fsencode(worktree) else {
            return Ok(None);
        };
        let mut file = pure(&bytes);
        if !file.ends_with(b"/") {
            file.push(b'/');
        }
        file.extend_from_slice(b".git");
        if !StdFs.stat(&file).is_ok_and(|s| s.kind == Kind::Regular) {
            return Ok(None);
        }
        let Ok(mut opened) = StdFs.open_waiting(&file) else {
            return Ok(None);
        };
        let mut bytes = Vec::new();
        let complete = loop {
            match StdFs.read(&mut opened, 64 * 1024) {
                Ok(chunk) if chunk.is_empty() => break true,
                Ok(chunk) => bytes.extend_from_slice(&chunk),
                Err(_) => break false,
            }
        };
        let closed = StdFs.close(opened).is_ok();
        Ok((complete && closed).then_some(bytes))
    }

    fn resolve(&mut self, prefix: &[u32]) -> Result<Option<PyStr>, E> {
        use crate::fsdata::{
            realpath::{realpath, Unresolved},
            Flavour, StdFs,
        };
        use crate::fsutil::fsdecode;
        use crate::gitfile_rule::follow::fsencode;
        use crate::githead::discovery::pure;
        use std::os::unix::ffi::OsStrExt;
        let Some(raw) = fsencode(prefix) else {
            return Ok(None);
        };
        let path = pure(&raw);
        let cwd = if path.starts_with(b"/") {
            Vec::new()
        } else {
            let Ok(cwd) = std::env::current_dir() else {
                return Ok(None);
            };
            cwd.as_os_str().as_bytes().to_vec()
        };
        match realpath(&StdFs, Flavour::Posix, &path, &cwd) {
            Ok(value) => Ok(Some(fsdecode(std::ffi::OsStr::from_bytes(&value)))),
            Err(Unresolved::Raised(PyErr::Value)) => {
                Err(MatchError::Value("lstat: embedded null character in path").into())
            }
            Err(Unresolved::Raised(_)) => Ok(None),
            Err(Unresolved::NotPorted) => unreachable!("selected the POSIX resolver"),
        }
    }

    fn stat(&mut self, worktree: &[u32]) -> Result<(), Numbered> {
        use crate::fsdata::{Fs, OsError, StdFs};
        let bytes = crate::gitfile_rule::follow::fsencode(worktree)
            .ok_or_else(|| Numbered::from(OsError::value()))?;
        StdFs.stat_numbered(&bytes).map(|_| ())
    }

    fn indeterminate(&mut self, worktree: &[u32], error: Numbered) -> Result<(), E> {
        (self.debug)(worktree, error)
    }
}
