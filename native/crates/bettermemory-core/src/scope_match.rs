//! `scope_match.py`'s first unit (scope_match-1, redrawn): the report
//! types and the leaves the two detectors share. `_roots_of` picks each
//! scope's most common working directory less the degenerate roots, and
//! `_drop_ancestor_roots` drops a root another scope's root sits under;
//! `_home_alias` spells a path under the home with `~`; `_boundary_after`
//! and `_find_root_occurrence` find a root cited in a body as a whole path;
//! `_declared_root_spans` and `_declared_root_covers` find where a declared
//! scope's root already explains a citation; `_trim_evidence` cuts the
//! evidence shown for a match. The two detectors, the three collectors and
//! the store read are scope_match-2 (native/README.md, "Scope mismatch's
//! leaves").
//!
//! Text is code points, as a Python `str` is, and every index counts code
//! points. The reference's effects are arguments: `home` is what `~`
//! expands to (`$HOME`, else the running user's password entry; None when
//! neither is known), as `verify_candidates::scan` takes it, and `probe`
//! answers `verify_candidates._home_ignores_case` of a path
//! ([`volume_probe`] asks a file system). These are `PosixPath` rules
//! (`os.sep` `/`, `os.altsep` None): on a target that is not Unix every
//! function that reads a separator or the home declines
//! ([`ScopeError::NotPorted`]).

use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;

use crate::fsdata::Fs;
use crate::pyre::atom::escape;
use crate::pyre::fold::Lit;
use crate::pyre::regex::{Regex, RegexError};
use crate::pyre::tree::{IGNORECASE, UNICODE};
use crate::pyvalue::PyStr;
use crate::unicode::{is_alnum, is_space, lower, py_strip};
use crate::verify_candidates::keys::home_ignores_case;
use crate::verify_candidates::scan::{normalize_for_compare, CompareError, HomeError, UserHome};
use crate::verify_candidates::validate::fold_altsep;

const SLASH: u32 = '/' as u32;
const TILDE: u32 = '~' as u32;
const SPACE: u32 = ' ' as u32;
const NEWLINE: u32 = '\n' as u32;
const DOT: u32 = '.' as u32;
/// `os.sep` on POSIX.
const SEP: [u32; 1] = [SLASH];
/// `os.altsep` on POSIX.
const ALTSEP: Option<&[u32]> = None;

/// `_MAX_MATCHES`: the evidence a report carries at most.
pub const MAX_MATCHES: usize = 5;

/// `_NOISY_NAME_STOPLIST`, in code point order.
pub const NOISY_NAME_STOPLIST: [&str; 15] = [
    "blog", "build", "config", "configs", "data", "doc", "docs", "note", "notes", "script",
    "scripts", "site", "src", "test", "tests",
];

/// `_trim_evidence`'s default `padding`.
pub const EVIDENCE_PADDING: usize = 24;

/// `ScopeMismatchEntry.kind`: the two values the reference writes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EntryKind {
    ProjectName,
    ProjectRoot,
}

impl EntryKind {
    pub fn as_str(self) -> &'static str {
        match self {
            EntryKind::ProjectName => "project_name",
            EntryKind::ProjectRoot => "project_root",
        }
    }
}

/// `ScopeMismatchEntry`: one piece of evidence.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScopeMismatchEntry {
    pub kind: EntryKind,
    pub evidence: PyStr,
    pub suggested_scope: PyStr,
}

impl ScopeMismatchEntry {
    /// `to_dict()`: the three fields in the reference's key order.
    pub fn to_dict(&self) -> [(&'static str, PyStr); 3] {
        [
            ("kind", self.kind.as_str().chars().map(u32::from).collect()),
            ("evidence", self.evidence.clone()),
            ("suggested_scope", self.suggested_scope.clone()),
        ]
    }
}

/// `ScopeMismatchReport`: the evidence in order and the suggested scopes.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ScopeMismatchReport {
    pub matches: Vec<ScopeMismatchEntry>,
    pub suggested_scopes: Vec<PyStr>,
}

impl ScopeMismatchReport {
    /// `has_mismatch`: any evidence at all.
    pub fn has_mismatch(&self) -> bool {
        !self.matches.is_empty()
    }
}

/// What a leaf answers instead of a value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ScopeError {
    /// `Path.home()`'s `RuntimeError`: no home is known, or it expands to
    /// a text that starts with `~`.
    Home(HomeError),
    /// `IndexError` out of `counter.most_common(1)[0]` for an empty count.
    Index,
    /// The engine does not build a needle's pattern: a root or alias of
    /// more code points than its arena holds (`pyre::lower::MAX_NODES`).
    /// The reference builds it.
    Regex(RegexError),
    /// The Windows flavour, or a probe that declines.
    NotPorted,
}

/// `_home_ignores_case(path)`, as the leaves ask it.
pub type Probe<'a> = dyn FnMut(&[u32]) -> Result<bool, ScopeError> + 'a;

/// The probe of `fs`: `verify_candidates::keys::home_ignores_case`, whose
/// only error is its decline.
pub fn volume_probe<F: Fs + ?Sized>(fs: &F) -> impl FnMut(&[u32]) -> Result<bool, ScopeError> + '_ {
    move |path| home_ignores_case(fs, path).map_err(|_| ScopeError::NotPorted)
}

fn posix() -> Result<(), ScopeError> {
    if cfg!(unix) {
        Ok(())
    } else {
        Err(ScopeError::NotPorted)
    }
}

/// `_fold_altsep(p, os.sep, os.altsep)`: the identity on POSIX.
fn fold(p: &[u32]) -> PyStr {
    fold_altsep(p, &SEP, ALTSEP)
}

/// `text[from:to]` with Python's clamping.
fn slice(text: &[u32], from: usize, to: usize) -> &[u32] {
    let to = to.min(text.len());
    &text[from.min(to)..to]
}

/// `_fold_altsep(str(Path.home()), os.sep, os.altsep)`: `PosixPath`'s
/// `expanduser` of `~` spelled as `str` spells it.
pub fn folded_home(home: Option<&[u32]>) -> Result<PyStr, ScopeError> {
    posix()?;
    let mut unknown = |_: &[u32]| UserHome::Unknown;
    match normalize_for_compare(&[TILDE], home, &mut unknown) {
        Ok(spelled) => Ok(fold(&spelled)),
        Err(CompareError::Home(e)) => Err(ScopeError::Home(e)),
        Err(CompareError::NotPorted) => Err(ScopeError::NotPorted),
    }
}

/// `counter.most_common(1)[0][0]`: the highest count, the first counted on
/// a tie (`heapq.nlargest(1, ...)` is `max`).
fn most_common(counter: &[(PyStr, u64)]) -> Option<&[u32]> {
    let mut best: Option<(&[u32], u64)> = None;
    for (value, count) in counter {
        if best.is_none_or(|(_, top)| *count > top) {
            best = Some((value, *count));
        }
    }
    best.map(|(value, _)| value)
}

/// `_roots_of(by_scope)`: each scope's most common working directory, in
/// the dict's order, less a root equal to the home or `/` after the fold,
/// and less one equal to the home modulo `str.lower` when the home's
/// volume folds case (probed once, and only when such a root appears);
/// then [`drop_ancestor_roots`]. `by_scope` is the dict's items in
/// insertion order, each count a `Counter`'s items in insertion order
/// (distinct keys). The home is read first, so an unknown one raises
/// even for no scopes.
pub fn roots_of(
    by_scope: &[(PyStr, Vec<(PyStr, u64)>)],
    home: Option<&[u32]>,
    probe: &mut Probe<'_>,
) -> Result<Vec<(PyStr, PyStr)>, ScopeError> {
    let home = folded_home(home)?;
    let home_lower = lower(&home);
    let mut home_folds_case: Option<bool> = None;
    let mut out = Vec::new();
    for (scope, counter) in by_scope {
        let root = most_common(counter).ok_or(ScopeError::Index)?;
        let folded = fold(root);
        if folded == home || folded == SEP {
            continue;
        }
        if lower(&folded) == home_lower {
            let folds = match home_folds_case {
                Some(folds) => folds,
                None => *home_folds_case.insert(probe(&home)?),
            };
            if folds {
                continue;
            }
        }
        out.push((scope.clone(), root.to_vec()));
    }
    drop_ancestor_roots(out)
}

/// `_drop_ancestor_roots(roots)`: under two roots, `roots` as given; else
/// each root whose folded spelling, less trailing `/`, plus `/` starts
/// another scope's (so `/` itself, folded to empty, is the ancestor of
/// every absolute root, and equal roots keep each other). `roots` is the
/// dict's items in order (distinct scopes).
pub fn drop_ancestor_roots(roots: Vec<(PyStr, PyStr)>) -> Result<Vec<(PyStr, PyStr)>, ScopeError> {
    posix()?;
    if roots.len() < 2 {
        return Ok(roots);
    }
    let folded: Vec<PyStr> = roots
        .iter()
        .map(|(_, root)| {
            let mut f = fold(root);
            let keep = f.iter().rposition(|&c| c != SLASH).map_or(0, |i| i + 1);
            f.truncate(keep);
            f
        })
        .collect();
    let under = |mine: &[u32], other: &[u32]| {
        other != mine
            && other.len() > mine.len()
            && other.starts_with(mine)
            && other[mine.len()] == SLASH
    };
    Ok(roots
        .iter()
        .enumerate()
        .filter(|&(i, _)| {
            !folded
                .iter()
                .enumerate()
                .any(|(j, other)| j != i && under(&folded[i], other))
        })
        .map(|(_, pair)| pair.clone())
        .collect())
}

/// `"~" + path[len(home):]`, the tail in `path`'s own spelling.
fn contract(path: &[u32], cut: usize) -> PyStr {
    let mut out = vec![TILDE];
    out.extend_from_slice(slice(path, cut, path.len()));
    out
}

/// `_home_alias(path)`: the folded path under `home + "/"` is `~` and its
/// tail; a prefix equal to that only modulo `str.lower` (the path cut at
/// the prefix's raw length first) gives the same when the home's volume
/// folds case, probed at each call that gets that far; else None. The home
/// itself has no alias.
pub fn home_alias(
    path: &[u32],
    home: Option<&[u32]>,
    probe: &mut Probe<'_>,
) -> Result<Option<PyStr>, ScopeError> {
    let home = folded_home(home)?;
    let folded = fold(path);
    let mut prefix = home.clone();
    prefix.push(SLASH);
    if folded.starts_with(&prefix) {
        return Ok(Some(contract(path, home.len())));
    }
    if lower(slice(&folded, 0, prefix.len())) == lower(&prefix) && probe(&home)? {
        return Ok(Some(contract(path, home.len())));
    }
    Ok(None)
}

/// `_boundary_after(body, end)`: past the end is a boundary; an
/// alphanumeric (`str.isalnum`), `-` or `_` is not; nor is a `.` followed
/// by a code point `str.isspace` refuses.
pub fn boundary_after(body: &[u32], end: usize) -> bool {
    let Some(&ch) = body.get(end) else {
        return true;
    };
    if is_alnum(ch) || ch == '-' as u32 || ch == '_' as u32 {
        return false;
    }
    !(ch == DOT && body.get(end + 1).is_some_and(|&next| !is_space(next)))
}

/// Every start of `needle` in `hay`, ascending, overlapping ones included:
/// the indexes `str.find` gives from each found index plus one (every
/// index up to the length for an empty needle). Knuth-Morris-Pratt, so the
/// whole walk is linear.
fn occurrences<'a>(hay: &'a [u32], needle: &'a [u32]) -> impl Iterator<Item = usize> + 'a {
    let mut fail = vec![0usize; needle.len()];
    let mut k = 0;
    for i in 1..needle.len() {
        while k > 0 && needle[i] != needle[k] {
            k = fail[k - 1];
        }
        if needle[i] == needle[k] {
            k += 1;
        }
        fail[i] = k;
    }
    let (mut at, mut matched) = (0usize, 0usize);
    std::iter::from_fn(move || {
        if needle.is_empty() {
            at += 1;
            return (at <= hay.len() + 1).then_some(at - 1);
        }
        while at < hay.len() {
            let c = hay[at];
            at += 1;
            while matched > 0 && c != needle[matched] {
                matched = fail[matched - 1];
            }
            if c == needle[matched] {
                matched += 1;
            }
            if matched == needle.len() {
                matched = fail[matched - 1];
                return Some(at - needle.len());
            }
        }
        None
    })
}

/// `_find_root_occurrence(body, needle)`: the first occurrence whose code
/// point before is not alphanumeric and after which [`boundary_after`]
/// holds, as `(start, end)`.
pub fn find_root_occurrence(body: &[u32], needle: &[u32]) -> Option<(usize, usize)> {
    occurrences(body, needle)
        .find(|&idx| {
            !(idx > 0 && is_alnum(body[idx - 1])) && boundary_after(body, idx + needle.len())
        })
        .map(|idx| (idx, idx + needle.len()))
}

/// `re.compile(re.escape(needle), re.IGNORECASE)`, or the engine's
/// decline of it.
fn needle_pattern(needle: &[u32]) -> Result<Regex, ScopeError> {
    Regex::new(&escape(needle), IGNORECASE).map_err(ScopeError::Regex)
}

/// The starts `_declared_root_spans` walks for one needle: every index at
/// which `re.compile(re.escape(needle), re.IGNORECASE)` matches `hay`,
/// ascending (the reference searches again from each start plus one, so
/// overlapping matches count). Each literal of the escaped needle matches
/// one code point, so a match is `needle.len()` long. The pattern is built
/// first, so a needle past the engine's arena declines as the engine does.
///
/// The leading run of literals `_compile` emits as LITERAL (code points
/// `_sre.unicode_iscased` refuses: `/`, `~`, digits, `.`, `-`) is the
/// literal prefix `_sre` searches with an overlap table: those code points
/// match only themselves. Here that prefix is walked with
/// Knuth-Morris-Pratt and only the rest of the needle is tried, literal by
/// literal ([`Lit::matches`], the engine's own test), at each prefix
/// occurrence. A needle that starts with a cased code point has no such
/// prefix and is searched by the engine from each start, as `_sre`
/// searches it ([`ignorecase_starts_by_engine`]).
#[doc(hidden)]
pub fn ignorecase_starts(hay: &[u32], needle: &[u32]) -> Result<Vec<usize>, ScopeError> {
    let pattern = needle_pattern(needle)?;
    let lits: Vec<Lit> = needle
        .iter()
        .map(|&c| Lit::new(c, IGNORECASE | UNICODE).expect("a str pattern folds by Unicode"))
        .collect();
    let exact = lits
        .iter()
        .take_while(|lit| matches!(lit, Lit::Exact(_)))
        .count();
    if exact == 0 {
        return Ok(search_from_each(&pattern, hay));
    }
    let rest = &lits[exact..];
    let mut starts = Vec::new();
    for at in occurrences(hay, &needle[..exact]) {
        if at + needle.len() > hay.len() {
            break;
        }
        let tail = &hay[at + exact..at + needle.len()];
        if rest.iter().zip(tail).all(|(lit, &c)| lit.matches(c)) {
            starts.push(at);
        }
    }
    Ok(starts)
}

/// [`ignorecase_starts`] by the engine alone: `pattern.search(hay, start)`
/// from 0 and from each start plus one, as the reference loops. The tests
/// hold the two to the same answers.
#[doc(hidden)]
pub fn ignorecase_starts_by_engine(hay: &[u32], needle: &[u32]) -> Result<Vec<usize>, ScopeError> {
    Ok(search_from_each(&needle_pattern(needle)?, hay))
}

fn search_from_each(pattern: &Regex, hay: &[u32]) -> Vec<usize> {
    let mut starts = Vec::new();
    let mut start = 0usize;
    while let Some(m) = pattern
        .search(hay, start as isize, hay.len() as isize)
        .expect("a str pattern searches a str")
    {
        let (a, _) = m.span(0).expect("group 0 takes part");
        let a = a as usize;
        starts.push(a);
        start = a + 1;
    }
    starts
}

/// `_declared_root_spans(body, declared, project_roots)`: over the folded
/// body, for each declared scope with a non-empty root, the root and its
/// [`home_alias`] as needles; each match of
/// `re.compile(re.escape(needle), re.IGNORECASE)` from `m.start() + 1` on
/// (so overlapping ones count, [`ignorecase_starts`]) is kept when its
/// text is the needle's, else only when the root's volume folds case
/// (probed once per scope, at the first such match). `declared` is a set
/// in the reference: a scope listed twice here is read once, at its first
/// place. The set is iterated in the hash seed's order, so the spans are
/// answered sorted; `project_roots` is the dict's items (a later key
/// wins).
pub fn declared_root_spans(
    body: &[u32],
    declared: &[PyStr],
    project_roots: &[(PyStr, PyStr)],
    home: Option<&[u32]>,
    probe: &mut Probe<'_>,
) -> Result<Vec<(usize, usize)>, ScopeError> {
    posix()?;
    let roots: HashMap<&[u32], &[u32]> = project_roots
        .iter()
        .map(|(scope, root)| (scope.as_slice(), root.as_slice()))
        .collect();
    let mut seen: HashSet<&[u32]> = HashSet::new();
    let folded_body = fold(body);
    let mut spans = Vec::new();
    for scope in declared {
        if !seen.insert(scope.as_slice()) {
            continue;
        }
        let Some(&root) = roots.get(scope.as_slice()) else {
            continue;
        };
        if root.is_empty() {
            continue;
        }
        let mut needles = vec![root.to_vec()];
        if let Some(alias) = home_alias(root, home, probe)? {
            needles.push(alias);
        }
        let mut root_folds_case: Option<bool> = None;
        for needle in &needles {
            let folded_needle = fold(needle);
            for a in ignorecase_starts(&folded_body, &folded_needle)? {
                let b = a + folded_needle.len();
                if folded_body[a..b] != folded_needle[..] {
                    let folds = match root_folds_case {
                        Some(folds) => folds,
                        None => *root_folds_case.insert(probe(root)?),
                    };
                    if !folds {
                        continue;
                    }
                }
                spans.push((a, b));
            }
        }
    }
    spans.sort_unstable();
    Ok(spans)
}

/// `_declared_root_covers(body, idx, root, declared_roots, contracted=...)`:
/// over the declared roots in code point order (a set: duplicates once),
/// one longer than `root` that extends it (bytes, else modulo `str.lower`
/// when the declared root's own volume folds case), spelled as itself or,
/// when `contracted`, by its [`home_alias`] (None skips it), cited at
/// `idx` (bytes, else modulo `str.lower` with the same probe, asked again)
/// and followed by a [`boundary_after`] on the raw body.
pub fn declared_root_covers(
    body: &[u32],
    idx: usize,
    root: &[u32],
    declared_roots: &[PyStr],
    contracted: bool,
    home: Option<&[u32]>,
    probe: &mut Probe<'_>,
) -> Result<bool, ScopeError> {
    posix()?;
    let folded_root = fold(root);
    let mut sorted: Vec<&[u32]> = declared_roots.iter().map(Vec::as_slice).collect();
    sorted.sort_unstable();
    sorted.dedup();
    for dr in sorted {
        let folded_dr = fold(dr);
        if folded_dr.len() <= folded_root.len() {
            continue;
        }
        if !folded_dr.starts_with(&folded_root)
            && (lower(&folded_dr[..folded_root.len()]) != lower(&folded_root) || !probe(dr)?)
        {
            continue;
        }
        let spelled = if contracted {
            match home_alias(dr, home, probe)? {
                Some(alias) => alias,
                None => continue,
            }
        } else {
            dr.to_vec()
        };
        let end = idx.saturating_add(spelled.len());
        let folded_cited = fold(slice(body, idx, end));
        let folded_spelled = fold(&spelled);
        if folded_cited != folded_spelled
            && (lower(&folded_cited) != lower(&folded_spelled) || !probe(dr)?)
        {
            continue;
        }
        if boundary_after(body, end) {
            return Ok(true);
        }
    }
    Ok(false)
}

/// `re.compile(r"\s+")`, `_trim_evidence`'s pattern.
fn runs_of_space() -> &'static Regex {
    static BUILT: OnceLock<Regex> = OnceLock::new();
    BUILT.get_or_init(|| Regex::new_str(r"\s+", 0).expect("the engine builds \\s+"))
}

/// `_trim_evidence(body, start, end, padding=padding)`: the span widened
/// by `padding` on each side and clamped, newlines as spaces,
/// `str.strip`, each run of `\s` as one space, `...` on each side cut.
pub fn trim_evidence(body: &[u32], start: usize, end: usize, padding: usize) -> PyStr {
    let lo = start.saturating_sub(padding);
    let hi = end.saturating_add(padding).min(body.len());
    let chunk: PyStr = slice(body, lo, hi)
        .iter()
        .map(|&c| if c == NEWLINE { SPACE } else { c })
        .collect();
    let chunk = runs_of_space()
        .sub(&[SPACE], py_strip(&chunk), 0)
        .expect("a literal replacement without a backslash");
    let mut out = Vec::with_capacity(chunk.len() + 6);
    if lo > 0 {
        out.extend([DOT; 3]);
    }
    out.extend(chunk);
    if hi < body.len() {
        out.extend([DOT; 3]);
    }
    out
}
