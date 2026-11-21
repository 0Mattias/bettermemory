//! `origin_dates.py`'s git-asking leaves (lines at v9): the reads of a
//! head's author dates (`_read_author_timestamps` 251-297,
//! `_hashed_seconds` 300-309), why `git log HEAD` was refused
//! (`_HEAD_PROBES` 312, `_refused_head` 315-341, `_unborn_or_broken`
//! 344-376, `_unread_entry` 379-392) and the extension of a remembered
//! history (`_extended_author_timestamps` 395-429, `_marked_seconds`
//! 432-439).
//!
//! Every git process is the caller's [`OriginGit`] (`origin._git`,
//! `origin._git_result`, the thread's counts and `_git.note_failed`), and
//! every read of the files or of git's discovery is the caller's
//! [`DateFiles`]. [`RefLeaves`] answers the files' half through
//! `githead::head_unborn` and `githead::packed_names`.

use std::collections::BTreeSet;

use crate::datetime::{git_seconds, DateTime};
use crate::githead::{self, GitDir, RefFs};
use crate::origin_history::{author_dates, read_lines, unanswered, HistoryUnread};
use crate::pyvalue::PyStr;

/// `origin._WALK_MARK` (origin.py 1339).
pub const WALK_MARK: &str = "\u{1}";

/// `_HEAD_PROBES`: the plumbing `refused_head` runs, in order.
pub const HEAD_PROBES: [&[&str]; 2] = [
    &["rev-parse", "--git-dir"],
    &["rev-parse", "--verify", "-q", "HEAD"],
];

/// The timeout every call here runs at.
pub const READ_TIMEOUT: f64 = 5.0;

/// A completed process as `origin._git_result` returns it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GitResult {
    pub returncode: i64,
    pub stdout: PyStr,
}

/// The git runner `origin` holds.
pub trait OriginGit {
    /// `origin._git(cwd, *args, timeout=, verbatim=, empty_ok=)`.
    fn git(
        &mut self,
        cwd: &[u8],
        args: &[PyStr],
        timeout: f64,
        verbatim: bool,
        empty_ok: bool,
    ) -> Option<PyStr>;
    /// `origin._git_result(cwd, *args, timeout=)`.
    fn git_result(&mut self, cwd: &[u8], args: &[PyStr], timeout: f64) -> Option<GitResult>;
    /// `origin_history.git_counts()`: the thread's timeouts and missing
    /// calls.
    fn counts(&self) -> (i64, i64);
    /// `_git.note_failed()`.
    fn note_failed(&mut self);
}

/// The reads of files and of git's discovery `refused_head` makes.
pub trait DateFiles {
    /// `githead.gitdir_at(cwd)`.
    fn gitdir_at(&mut self, cwd: &[u8]) -> Option<GitDir>;
    /// `githead.locate(cwd)`.
    fn locate(&mut self, cwd: &[u8]) -> Option<GitDir>;
    /// `githead.head_unborn(gd)`.
    fn head_unborn(&mut self, gd: &GitDir) -> Option<bool>;
    /// `githead.packed_names(gd, name)`.
    fn packed_names(&mut self, gd: &GitDir, name: &[u32]) -> bool;
    /// `gitfile_rule.discovery_refusal(cwd) is not None`.
    fn discovery_refusal(&mut self, cwd: &[u8]) -> bool;
    /// `githead.entry_above(cwd)`.
    fn entry_above(&mut self, cwd: &[u8]) -> bool;
}

/// A cwd's lookup, as a boxed closure.
pub type Lookup<'a, T> = Box<dyn FnMut(&[u8]) -> T + 'a>;

/// [`DateFiles`] over a [`RefFs`] for the files and closures for
/// discovery (`githead::discovery`'s `gitdir_at`, `locate` and
/// `any_entry_above`, `gitfile_rule::discovery::discovery_refusal`).
pub struct RefLeaves<'a> {
    pub fs: &'a dyn RefFs,
    pub gitdir_at: Lookup<'a, Option<GitDir>>,
    pub locate: Lookup<'a, Option<GitDir>>,
    pub discovery_refusal: Lookup<'a, bool>,
    pub entry_above: Lookup<'a, bool>,
}

impl DateFiles for RefLeaves<'_> {
    fn gitdir_at(&mut self, cwd: &[u8]) -> Option<GitDir> {
        (self.gitdir_at)(cwd)
    }
    fn locate(&mut self, cwd: &[u8]) -> Option<GitDir> {
        (self.locate)(cwd)
    }
    fn head_unborn(&mut self, gd: &GitDir) -> Option<bool> {
        githead::head_unborn(self.fs, gd)
    }
    fn packed_names(&mut self, gd: &GitDir, name: &[u32]) -> bool {
        // A name git printed is UTF-8 text; a lone surrogate (never read
        // from git) has no bytes and names nothing.
        let text: Option<String> = name.iter().map(|&c| char::from_u32(c)).collect();
        text.is_some_and(|t| githead::packed_names(self.fs, gd, t.as_bytes()))
    }
    fn discovery_refusal(&mut self, cwd: &[u8]) -> bool {
        (self.discovery_refusal)(cwd)
    }
    fn entry_above(&mut self, cwd: &[u8]) -> bool {
        (self.entry_above)(cwd)
    }
}

/// What a read of author dates answers: the dates, or why none were read
/// (`Err(None)` where no history applies).
pub type Dates = Result<Vec<DateTime>, Option<HistoryUnread>>;

fn cps(text: &str) -> PyStr {
    text.chars().map(u32::from).collect()
}

fn argv(args: &[&str]) -> Vec<PyStr> {
    args.iter().map(|a| cps(a)).collect()
}

/// `origin.is_full_commit_sha` over text: 40 or 64 lowercase hex digits.
pub fn is_full_commit_sha(text: &[u32]) -> bool {
    (text.len() == 40 || text.len() == 64)
        && text
            .iter()
            .all(|&c| (0x30..=0x39).contains(&c) || (0x61..=0x66).contains(&c))
}

/// `str.partition(" ")`: before the first space and after it (empty
/// where there is none).
fn partition(line: &[u32]) -> (&[u32], &[u32]) {
    match line.iter().position(|&c| c == 0x20) {
        Some(i) => (&line[..i], &line[i + 1..]),
        None => (line, &[]),
    }
}

fn starts_with(text: &[u32], prefix: &str) -> bool {
    let p = cps(prefix);
    text.len() >= p.len() && text[..p.len()] == p[..]
}

/// `_read_author_timestamps(cwd, revision, cut)`.
pub fn read_author_timestamps(
    git: &mut dyn OriginGit,
    files: &mut dyn DateFiles,
    cwd: &[u8],
    revision: &[u32],
    cut: &BTreeSet<PyStr>,
) -> Dates {
    let before = git.counts();
    let form = if cut.is_empty() {
        "--format=%at"
    } else {
        "--format=%H %at"
    };
    let args = vec![cps("log"), cps(form), revision.to_vec()];
    let Some(raw) = git.git(cwd, &args, READ_TIMEOUT, true, false) else {
        let named = is_full_commit_sha(revision);
        if !named && git.counts() == before {
            return Err(refused_head(git, files, cwd));
        }
        return Err(unanswered(before, git.counts(), named));
    };
    let values: Option<Vec<i64>> = if cut.is_empty() {
        read_lines(&raw, git_seconds)
    } else {
        let pairs = read_lines(&raw, hashed_seconds);
        if let Some(pairs) = &pairs {
            if pairs.iter().any(|(sha, _)| cut.contains(sha)) {
                git.note_failed();
                return Err(Some(HistoryUnread::Shallow));
            }
        }
        pairs.map(|p| p.into_iter().map(|(_, v)| v).collect())
    };
    match values {
        Some(values) if !values.is_empty() => {
            let wide: Vec<i128> = values.into_iter().map(i128::from).collect();
            Ok(author_dates(&wide))
        }
        _ => {
            git.note_failed();
            Err(Some(HistoryUnread::Error))
        }
    }
}

/// `_hashed_seconds(line)`: a `%H %at` line as (hash, seconds).
pub fn hashed_seconds(line: &[u32]) -> Option<(PyStr, i64)> {
    let (sha, stamp) = partition(line);
    let value = git_seconds(stamp);
    if !is_full_commit_sha(sha) {
        return None;
    }
    Some((sha.to_vec(), value?))
}

/// `_refused_head(cwd)`.
pub fn refused_head(
    git: &mut dyn OriginGit,
    files: &mut dyn DateFiles,
    cwd: &[u8],
) -> Option<HistoryUnread> {
    for (i, probe) in HEAD_PROBES.iter().enumerate() {
        let before = git.counts();
        if git
            .git(cwd, &argv(probe), READ_TIMEOUT, false, false)
            .is_some()
        {
            continue;
        }
        if git.counts() != before {
            return unanswered(before, git.counts(), false);
        }
        if i == 0 {
            return unread_entry(files, cwd);
        }
        return unborn_or_broken(git, files, cwd);
    }
    Some(HistoryUnread::Error)
}

/// `_unborn_or_broken(cwd)`.
pub fn unborn_or_broken(
    git: &mut dyn OriginGit,
    files: &mut dyn DateFiles,
    cwd: &[u8],
) -> Option<HistoryUnread> {
    let gd = files.gitdir_at(cwd).or_else(|| files.locate(cwd));
    let unborn = gd.as_ref().and_then(|gd| files.head_unborn(gd));
    if let Some(unborn) = unborn {
        return if unborn {
            None
        } else {
            Some(HistoryUnread::Error)
        };
    }
    let before = git.counts();
    let name = git.git(
        cwd,
        &argv(&["symbolic-ref", "-q", "HEAD"]),
        READ_TIMEOUT,
        false,
        false,
    );
    let Some(name) = name.filter(|n| starts_with(n, "refs/")) else {
        return unanswered(before, git.counts(), true);
    };
    let args = vec![cps("show-ref"), cps("--exists"), name.clone()];
    let Some(shown) = git.git_result(cwd, &args, READ_TIMEOUT) else {
        return unanswered(before, git.counts(), true);
    };
    if shown.returncode != 2 {
        return Some(HistoryUnread::Error);
    }
    let named = gd.as_ref().is_some_and(|gd| files.packed_names(gd, &name));
    named.then_some(HistoryUnread::Error)
}

/// `_unread_entry(cwd)`.
pub fn unread_entry(files: &mut dyn DateFiles, cwd: &[u8]) -> Option<HistoryUnread> {
    if files.discovery_refusal(cwd) {
        return None;
    }
    files.entry_above(cwd).then_some(HistoryUnread::Error)
}

/// `_extended_author_timestamps(cwd, base_head, base, head, cut)`:
/// `Ok(None)` where the caller reads the history whole. The dates sort
/// by instant; `base` holds aware dates, as the reference's caller gives.
pub fn extended_author_timestamps(
    git: &mut dyn OriginGit,
    cwd: &[u8],
    base_head: &[u32],
    base: &[DateTime],
    head: &[u32],
    cut: &BTreeSet<PyStr>,
) -> Result<Option<Vec<DateTime>>, HistoryUnread> {
    let mut range = base_head.to_vec();
    range.extend(cps(".."));
    range.extend_from_slice(head);
    let args = vec![
        cps("log"),
        cps("--boundary"),
        cps("--format=%m%H %at"),
        range,
    ];
    let Some(result) = git.git_result(cwd, &args, READ_TIMEOUT) else {
        return Ok(None);
    };
    if result.returncode != 0 {
        return Ok(None);
    }
    let Some(lines) = read_lines(&result.stdout, marked_seconds) else {
        return Ok(None);
    };
    if !lines
        .iter()
        .any(|(listed, sha, value)| !listed && sha == base_head && *value == 0)
    {
        return Ok(None);
    }
    let added: Vec<&(bool, PyStr, i64)> = lines.iter().filter(|l| l.0).collect();
    if added.iter().any(|(_, sha, _)| cut.contains(sha)) {
        git.note_failed();
        return Err(HistoryUnread::Shallow);
    }
    let wide: Vec<i128> = added.iter().map(|l| i128::from(l.2)).collect();
    let mut all = base.to_vec();
    all.extend(author_dates(&wide));
    all.sort_by_key(|d| d.instant_us().unwrap_or_else(|| d.wall_micros()));
    Ok(Some(all))
}

/// `_marked_seconds(line)`: a `%m%H %at` line as (listed, hash, seconds),
/// a boundary commit's seconds read as 0.
pub fn marked_seconds(line: &[u32]) -> Option<(bool, PyStr, i64)> {
    let mark = *line.first()?;
    let (sha, stamp) = partition(&line[1..]);
    let value = git_seconds(stamp);
    if ![0x2D, 0x3C, 0x3E].contains(&mark) || !is_full_commit_sha(sha) {
        return None;
    }
    let value = value?;
    let listed = mark != 0x2D;
    Some((listed, sha.to_vec(), if listed { value } else { 0 }))
}
