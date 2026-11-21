//! Unit 3 of `verify_candidates.py`: `_normalize_for_compare`,
//! `_scan_candidates` and `_extract_candidates` without its memo.
//!
//! The home directory is an argument: `home` is what `~` expands to
//! (`$HOME`, else the password entry of the running user; None when
//! neither is known), and `lookup` answers the home of a named user
//! (`pwd.getpwnam(name).pw_dir`, [`UserHome`]). The rules are those of
//! `PosixPath`: the Windows flavour is not ported, and on a target that is
//! not Unix both functions decline ([`CompareError::NotPorted`],
//! [`ScanError::NotPorted`]).

use std::collections::{HashMap, HashSet};
use std::fmt;

use super::patterns::{backtick_iter, bare_iter, domain_route_iter};
use super::validate::{bounded_scan_text, is_lower, normalize_candidate, IndexError};
use super::MAX_PATHS_PER_BODY;
use crate::unicode::is_space;

const SLASH: u32 = '/' as u32;
const TILDE: u32 = '~' as u32;
const DOT: u32 = '.' as u32;
const SPACE: u32 = ' ' as u32;

/// The `RuntimeError` `PosixPath.expanduser` raises when the home of `~`
/// or of `~user` cannot be determined (or expands to a text that itself
/// starts with `~`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct HomeError;

impl fmt::Display for HomeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Could not determine home directory.")
    }
}

/// What `normalize_for_compare` answers instead of a text: `expanduser`'s
/// `RuntimeError`, or a decline on a target that is not Unix, where the
/// reference spells the text through `WindowsPath`, which is not ported.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CompareError {
    Home(HomeError),
    NotPorted,
}

/// What escapes `_scan_candidates`: `_normalize_candidate`'s `IndexError`
/// or `expanduser`'s `RuntimeError`; or a decline on a target that is not
/// Unix.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScanError {
    Index(IndexError),
    Home(HomeError),
    NotPorted,
}

impl From<CompareError> for ScanError {
    fn from(e: CompareError) -> Self {
        match e {
            CompareError::Home(h) => ScanError::Home(h),
            CompareError::NotPorted => ScanError::NotPorted,
        }
    }
}

impl From<IndexError> for ScanError {
    fn from(e: IndexError) -> Self {
        ScanError::Index(e)
    }
}

impl From<HomeError> for ScanError {
    fn from(e: HomeError) -> Self {
        ScanError::Home(e)
    }
}

/// What `pwd.getpwnam(name)` answers: the entry's `pw_dir`, a `KeyError`
/// (no such user), or a `ValueError` before any lookup (a name holding NUL,
/// or one the filesystem encoding cannot encode: `UnicodeEncodeError` is a
/// `ValueError`). `posixpath.expanduser` catches only the `KeyError`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum UserHome {
    Found(Vec<u32>),
    Unknown,
    Refused,
}

/// The home of a named user, `pwd.getpwnam(name)`.
pub type UserLookup<'a> = dyn FnMut(&[u32]) -> UserHome + 'a;

/// Whether CPython's `pwd.getpwnam` raises `ValueError` for `name` before
/// it looks the name up, with a UTF-8 filesystem encoding and the
/// `surrogateescape` handler (macOS, and Linux under a UTF-8 or coerced C
/// locale): a NUL, or a surrogate outside U+DC80..U+DCFF. A lookup that
/// reads the host's user table answers [`UserHome::Refused`] for these.
pub fn getpwnam_refuses(name: &[u32]) -> bool {
    name.iter()
        .any(|&c| c == 0 || ((0xD800..=0xDFFF).contains(&c) && !(0xDC80..=0xDCFF).contains(&c)))
}

/// One candidate: `(path, drop_if_missing, bare_spaced)`.
pub type Candidate = (Vec<u32>, bool, bool);

/// `PurePosixPath._parse_path`: the root (`""`, `/`, or `//` for exactly
/// two leading slashes) and the parts, without empty and `.` parts.
fn parse_posix(p: &[u32]) -> (usize, Vec<&[u32]>) {
    let root = match p {
        [SLASH, SLASH, SLASH, ..] => 1,
        [SLASH, SLASH, ..] => 2,
        [SLASH, ..] => 1,
        _ => 0,
    };
    let rest = &p[root.min(p.len())..];
    let tail = rest
        .split(|&c| c == SLASH)
        .filter(|x| !x.is_empty() && *x != [DOT])
        .collect();
    (root, tail)
}

/// `str` of a parsed POSIX path: root, then the parts joined, or `.`.
fn format_posix(root: usize, tail: &[&[u32]]) -> Vec<u32> {
    let mut out = vec![SLASH; root];
    for (i, part) in tail.iter().enumerate() {
        if i > 0 {
            out.push(SLASH);
        }
        out.extend_from_slice(part);
    }
    if out.is_empty() {
        out.push(DOT);
    }
    out
}

/// `posixpath.expanduser` of one part that starts with `~` and holds no
/// slash: the part itself when the home is unknown, None when
/// `getpwnam` raises `ValueError`.
fn expand_part(
    part: &[u32],
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Option<Vec<u32>> {
    let userhome = if part.len() == 1 {
        home.map(<[u32]>::to_vec)
    } else {
        match lookup(&part[1..]) {
            UserHome::Found(h) => Some(h),
            UserHome::Unknown => None,
            UserHome::Refused => return None,
        }
    };
    let Some(userhome) = userhome else {
        return Some(part.to_vec());
    };
    let end = userhome
        .iter()
        .rposition(|&c| c != SLASH)
        .map_or(0, |i| i + 1);
    if end == 0 {
        return Some(vec![SLASH]);
    }
    Some(userhome[..end].to_vec())
}

/// `_normalize_for_compare`: `str(Path(raw).expanduser())` on POSIX, or
/// `""` for an empty text. A `~` or `~user` first part (after empty and
/// `.` parts are dropped, so `./~x` expands too) whose home is unknown,
/// or whose home starts with `~`, is `Err(CompareError::Home)`: the
/// reference catches only `OSError` and `ValueError`, so its
/// `RuntimeError` propagates. A `~user` name `getpwnam` refuses with
/// `ValueError` answers `raw` unchanged, as the reference's handler does.
/// A non-empty text on a target that is not Unix declines.
pub fn normalize_for_compare(
    raw: &[u32],
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Result<Vec<u32>, CompareError> {
    if raw.is_empty() {
        return Ok(Vec::new());
    }
    if cfg!(not(unix)) {
        return Err(CompareError::NotPorted);
    }
    let (root, tail) = parse_posix(raw);
    match tail.first() {
        Some(first) if root == 0 && first.first() == Some(&TILDE) => {
            let Some(homedir) = expand_part(first, home, lookup) else {
                return Ok(raw.to_vec());
            };
            if homedir.first() == Some(&TILDE) {
                return Err(CompareError::Home(HomeError));
            }
            let (hroot, mut htail) = parse_posix(&homedir);
            htail.extend_from_slice(&tail[1..]);
            Ok(format_posix(hroot, &htail))
        }
        _ => Ok(format_posix(root, &tail)),
    }
}

/// `str.strip()`: Python's whitespace off both ends.
fn py_strip(s: &[u32]) -> &[u32] {
    let Some(a) = s.iter().position(|&c| !is_space(c)) else {
        return &s[..0];
    };
    let b = s.iter().rposition(|&c| !is_space(c)).map_or(a, |i| i + 1);
    &s[a..b]
}

/// `candidate.split("/", 2)[1] in route_segments` for a candidate that
/// starts with `/`.
fn is_route(candidate: &[u32], routes: &HashSet<&[u32]>) -> bool {
    if candidate.first() != Some(&SLASH) || routes.is_empty() {
        return false;
    }
    let rest = &candidate[1..];
    let seg = rest
        .iter()
        .position(|&c| c == SLASH)
        .map_or(rest, |i| &rest[..i]);
    routes.contains(seg)
}

/// `_scan_candidates`: the body bounded, domain routes collected from it,
/// the backtick pass, the backtick spans masked with spaces, then the bare
/// pass with its ambiguity and spaced flags, deduplicated on
/// `normalize_for_compare` and capped at `MAX_PATHS_PER_BODY`.
pub fn scan_candidates(
    body: &[u32],
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Result<Vec<Candidate>, ScanError> {
    if body.is_empty() {
        return Ok(Vec::new());
    }
    if cfg!(not(unix)) {
        return Err(ScanError::NotPorted);
    }
    let body = bounded_scan_text(body);
    let routes: HashSet<&[u32]> = domain_route_iter(body)
        .iter()
        .map(|h| &body[h.group_start..h.group_end])
        .collect();
    let mut candidates: Vec<Candidate> = Vec::new();
    let mut index_of: HashMap<Vec<u32>, usize> = HashMap::new();
    let mut masked = body.to_vec();
    for hit in backtick_iter(body) {
        let raw = py_strip(&body[hit.group_start..hit.group_end]);
        if let Some(normalized) = normalize_candidate(raw)? {
            if !is_route(&normalized, &routes) {
                let key = normalize_for_compare(&normalized, home, lookup)?;
                if let std::collections::hash_map::Entry::Vacant(slot) = index_of.entry(key) {
                    slot.insert(candidates.len());
                    candidates.push((normalized, false, false));
                    if candidates.len() >= MAX_PATHS_PER_BODY {
                        return Ok(candidates);
                    }
                }
            }
        }
        masked[hit.start..hit.end].fill(SPACE);
    }
    for hit in bare_iter(&masked) {
        let raw = &masked[hit.group_start..hit.group_end];
        let Some(normalized) = normalize_candidate(raw)? else {
            continue;
        };
        if is_route(&normalized, &routes) {
            continue;
        }
        let tail = &masked[hit.group_end..(hit.group_end + 2).min(masked.len())];
        let ambiguous =
            normalized == raw && tail.len() == 2 && tail[0] == SPACE && !is_lower(tail[1]);
        let key = normalize_for_compare(&normalized, home, lookup)?;
        if let Some(&i) = index_of.get(&key) {
            if candidates[i].1 && !ambiguous {
                candidates[i].1 = false;
            }
            continue;
        }
        index_of.insert(key, candidates.len());
        let spaced = normalized.contains(&SPACE);
        candidates.push((normalized, ambiguous, spaced));
        if candidates.len() >= MAX_PATHS_PER_BODY {
            break;
        }
    }
    Ok(candidates)
}

/// `_extract_candidates` without the body parse memo: `[]` for an empty
/// body, else the scan. A memo wraps this function unchanged.
pub fn extract_candidates(
    body: &[u32],
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Result<Vec<Candidate>, ScanError> {
    if body.is_empty() {
        return Ok(Vec::new());
    }
    scan_candidates(body, home, lookup)
}
