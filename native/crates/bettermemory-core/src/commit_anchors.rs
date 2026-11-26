//! `commit_anchors.py` (110 lines at 998253b2): stored absolute commit
//! anchors projected through their checkout views, repository-relative
//! before the drift memo reads them.
//!
//! Paths are code points, as a Python `str` is. The process is an
//! argument ([`PathHost`]): `os.name` (whether `os.path` is `ntpath`) and
//! `os.getcwd()`, which `ntpath.relpath` reads off Windows for a root
//! that is not absolute. `os.path.expanduser` takes the home and the user
//! lookup as `verify_candidates`' scan takes them: `home` is what `~`
//! expands to (`$HOME`, else the running user's password entry; None
//! when neither is known) and `lookup` answers `pwd.getpwnam`.
//!
//! The Windows flavour declines ([`Failure::NotPorted`]) where the
//! reference asks Windows: `ntpath.relpath` there calls
//! `nt._getfullpathname`, and `ntpath.expanduser` reads `USERPROFILE`.
//! Everything before those calls (the `..` refusal, `normpath`, the
//! `inside` test) answers on every host, and `posixpath.relpath` of an
//! absolute root never asks the process.
//!
//! [`HostPaths`] is `verify_anchoring`'s [`AnchorPaths`] over these
//! functions. A `ClaimAnchor` takes `&str`, so a spelled path holding a
//! lone surrogate that must pass through a translating anchor declines.

use std::cell::RefCell;
use std::collections::HashSet;

use crate::attestation_paths::absolute_spelling;
use crate::capture::paths::inside;
use crate::fsdata::{Flavour, PyErr};
use crate::gitfile_rule::hostpath::text::{normpath, relpath, Process, Raised};
use crate::pyvalue::PyStr;
use crate::unicode::{cps, to_string};
use crate::verify_anchoring::anchor::{AnchorPaths, ClaimAnchor, GLOBAL_RECORD};
use crate::verify_candidates::scan::{UserHome, UserLookup};

/// `UNMAPPED_ANCHOR`.
pub const UNMAPPED_ANCHOR: &str = "unmapped_anchor";
/// `UNMAPPED_REASON`.
pub const UNMAPPED_REASON: &str = "a commit anchor has no recorded checkout view";

/// Whether this build's `os.path` is `ntpath` (`os.name == "nt"`).
pub const NT_HOST: bool = cfg!(windows);

const SLASH: u32 = b'/' as u32;
const BACKSLASH: u32 = b'\\' as u32;
const TILDE: u32 = b'~' as u32;
const DOT: u32 = b'.' as u32;

/// What escapes a function of this module.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    /// `os.getcwd()` raised inside `ntpath.relpath` (an `OSError`, which
    /// `relative_under` does not catch).
    Os(PyErr),
    /// `pwd.getpwnam` refused a `~user` name: `ValueError` out of
    /// `os.path.expanduser`.
    Value,
    /// The reference asks Windows here, or a lone surrogate would pass
    /// through a `ClaimAnchor`: a decline.
    NotPorted,
}

/// The process as this module reads it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PathHost {
    /// `os.name == "nt"`: [`NT_HOST`] for this build.
    pub nt: bool,
    /// `os.getcwd()`, or the `OSError` it raises.
    pub cwd: Result<PyStr, PyErr>,
}

impl PathHost {
    /// This build's flavour with the given working directory.
    pub fn here(cwd: Result<PyStr, PyErr>) -> PathHost {
        PathHost { nt: NT_HOST, cwd }
    }
}

impl Process for PathHost {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        self.cwd.clone()
    }
}

/// Whether `..` is a part of `path` split on `/`, and on `\` too where
/// `backslash` holds.
fn has_parent(path: &[u32], backslash: bool) -> bool {
    path.split(|&c| c == SLASH || (backslash && c == BACKSLASH))
        .any(|part| part == [DOT, DOT])
}

/// `relative_under(path, root)`: the spelling below `root` in the root's
/// flavour (`posixpath` for a root starting with `/`, else `ntpath`, its
/// answer with `/` for `\`), or None: no root, a `..` part, not inside,
/// or `relpath`'s `ValueError` (another drive).
pub fn relative_under(
    path: &[u32],
    root: Option<&[u32]>,
    host: &PathHost,
) -> Result<Option<PyStr>, Failure> {
    let Some(root) = root else { return Ok(None) };
    let posix = root.first() == Some(&SLASH);
    if has_parent(path, !posix) {
        return Ok(None);
    }
    let flavour = if posix {
        Flavour::Posix
    } else {
        Flavour::Windows
    };
    let (path, root) = (normpath(flavour, path), normpath(flavour, root));
    if !inside(&path, &root) {
        return Ok(None);
    }
    if !posix && host.nt {
        return Err(Failure::NotPorted);
    }
    match relpath(flavour, &path, &root, host) {
        Ok(mut relative) => {
            if !posix {
                for c in relative.iter_mut().filter(|c| **c == BACKSLASH) {
                    *c = SLASH;
                }
            }
            Ok(Some(relative))
        }
        Err(Raised::Value(_)) => Ok(None),
        Err(Raised::Os(e)) => Err(Failure::Os(e)),
    }
}

/// `expected_home(path, roots)`: the spelling names one of the roots.
pub fn expected_home<R: AsRef<[u32]>>(path: &[u32], roots: &[R]) -> bool {
    roots.iter().any(|root| inside(path, root.as_ref()))
}

/// `os.path.expanduser(path)`: `posixpath`'s over the given home and
/// lookup; `ntpath`'s declines on a path it would expand.
pub fn expanduser(
    path: &[u32],
    host: &PathHost,
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Result<PyStr, Failure> {
    if path.first() != Some(&TILDE) {
        return Ok(path.to_vec());
    }
    if host.nt {
        return Err(Failure::NotPorted);
    }
    let i = path[1..]
        .iter()
        .position(|&c| c == SLASH)
        .map_or(path.len(), |j| j + 1);
    let userhome = if i == 1 {
        match home {
            Some(h) => h.to_vec(),
            None => return Ok(path.to_vec()),
        }
    } else {
        match lookup(&path[1..i]) {
            UserHome::Found(h) => h,
            UserHome::Unknown => return Ok(path.to_vec()),
            UserHome::Refused => return Err(Failure::Value),
        }
    };
    let end = userhome
        .iter()
        .rposition(|&c| c != SLASH)
        .map_or(0, |k| k + 1);
    let mut out = userhome[..end].to_vec();
    out.extend_from_slice(&path[i..]);
    Ok(if out.is_empty() { vec![SLASH] } else { out })
}

/// `CommitAnchors`: the projected paths and whether a path expected
/// under the recorded home could not be placed there.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CommitAnchors {
    pub paths: Vec<PyStr>,
    pub unmapped: bool,
}

/// `verify_anchoring`'s path rules over this module: `expected_home`,
/// `relative_under` and `_paths.inside`. The trait answers no failure, so
/// the first one met is kept for [`HostPaths::failure`], and the call
/// that met it answers None.
pub struct HostPaths<'h> {
    host: &'h PathHost,
    failure: RefCell<Option<Failure>>,
}

impl<'h> HostPaths<'h> {
    pub fn new(host: &'h PathHost) -> HostPaths<'h> {
        HostPaths {
            host,
            failure: RefCell::new(None),
        }
    }

    /// The first failure a `relative_under` met since the last call,
    /// taken.
    pub fn failure(&self) -> Option<Failure> {
        self.failure.borrow_mut().take()
    }
}

impl AnchorPaths for HostPaths<'_> {
    fn expected_home(&self, path: &str, roots: &[PyStr]) -> bool {
        expected_home(&cps(path), roots)
    }

    fn relative_under(&self, path: &str, root: Option<&str>) -> Option<String> {
        let root = root.map(cps);
        match relative_under(&cps(path), root.as_deref(), self.host) {
            Ok(found) => found.and_then(|r| to_string(&r)),
            Err(e) => {
                self.failure.borrow_mut().get_or_insert(e);
                None
            }
        }
    }

    fn inside(&self, path: &str, root: &str) -> bool {
        inside(&cps(path), &cps(root))
    }
}

/// `project_commit_anchors(paths, anchor)`.
pub fn project_commit_anchors(
    paths: &[PyStr],
    anchor: Option<&ClaimAnchor>,
    host: &PathHost,
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Result<CommitAnchors, Failure> {
    let Some(anchor) = anchor.filter(|a| a.rule != GLOBAL_RECORD) else {
        return Ok(CommitAnchors {
            paths: paths.to_vec(),
            unmapped: false,
        });
    };
    let mut roots: Vec<PyStr> = anchor.recorded_roots.iter().map(|r| cps(r)).collect();
    // `sorted(..., key=len, reverse=True)` is stable: roots of one length
    // keep their recorded order.
    let mut longest_first = roots.clone();
    longest_first.sort_by_key(|r| std::cmp::Reverse(r.len()));
    if let Some(origin) = anchor.origin_root.as_ref().filter(|o| !o.is_empty()) {
        roots.push(origin.clone());
    }
    let root = anchor.root.as_deref().map(cps);
    let truthy_root = root.as_deref().filter(|r| !r.is_empty());
    let checks = HostPaths::new(host);
    let mut out: Vec<PyStr> = Vec::new();
    let mut unmapped = false;
    for path in paths {
        let spelled = expanduser(path, host, home, lookup)?;
        let expected = expected_home(&spelled, &roots);
        let mut recorded = None;
        for r in &longest_first {
            recorded = relative_under(&spelled, Some(r), host)?;
            if recorded.is_some() {
                break;
            }
        }
        if let Some(found) = recorded {
            out.push(found);
            continue;
        }
        if !absolute_spelling(path) && !expected {
            out.push(path.clone());
            continue;
        }
        if has_parent(&spelled, spelled.first() != Some(&SLASH)) {
            unmapped |= expected && !truthy_root.is_some_and(|r| inside(&spelled, r));
            out.push(path.clone());
            continue;
        }
        let target = if anchor.translate.is_none() {
            Some(spelled)
        } else {
            let text = to_string(&spelled).ok_or(Failure::NotPorted)?;
            let (target, _rule) = anchor.absolute(&text, &checks);
            if let Some(e) = checks.failure() {
                return Err(e);
            }
            target.map(|t| cps(&t))
        };
        let relative = match target {
            Some(t) => relative_under(&t, root.as_deref(), host)?,
            None => None,
        };
        match relative {
            Some(found) => out.push(found),
            None if expected => unmapped = true,
            None => out.push(path.clone()),
        }
    }
    let mut seen: HashSet<PyStr> = HashSet::new();
    out.retain(|p| seen.insert(p.clone()));
    Ok(CommitAnchors {
        paths: out,
        unmapped,
    })
}
