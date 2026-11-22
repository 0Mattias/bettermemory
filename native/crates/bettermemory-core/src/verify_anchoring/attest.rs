//! The attestation checks of `verify_anchoring.py`: which attested paths
//! the attesting machine cannot see (`unverifiable_attestations`,
//! `_unverifiable_at`), the anchoring predicates beside them
//! (`_is_absolute_attestation`, `_join_under`, `_resolves_under`,
//! `_worktree_root_is_live`) and the claim-key set of an attestation list
//! (`_normalize_attestations`).
//!
//! Effects are arguments. The host ([`Host`]) answers `os.name`,
//! `os.path.expanduser` and the `os.lstat` of `attestation_paths`; the
//! file system ([`Fs`]) answers `os.path.isdir` and the walk of
//! `os.path.realpath`; `_normalize_for_compare`, `_path_exists`,
//! `_absent_by_sparsity` and `_claim_key` are closures ([`Reads`]); the
//! anchor is an [`AnchorView`].

use std::collections::BTreeSet;

use crate::attestation_paths::{
    interpret_path, joined_path, path_mode, placeholder_spelling, Host, Mode, Paths, Raised,
};
use crate::config::pure_path;
use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{Flavour, Fs, Kind};
use crate::gitfile_rule::follow::fsencode;
use crate::gitfile_rule::hostpath::text::{isabs, join};
use crate::pyvalue::PyStr;
use crate::unicode;
use crate::verify_anchoring::anchor::CONVICTING_ANCHORS;
use crate::verify_candidates::validate::{escapes_root, normalize_candidate};

const SLASH: u32 = b'/' as u32;
const BACKSLASH: u32 = b'\\' as u32;

/// What `_unverifiable_at` reads of a `ClaimAnchor`: `root`,
/// `convicting`, `declared_root` (only whether it is None is read) and
/// `absolute(path)` (the path to stat, or None, and its rule).
pub trait AnchorView {
    fn root(&self) -> Option<PyStr>;
    fn convicting(&self) -> bool;
    fn declared_root(&self) -> Option<PyStr>;
    fn absolute(&self, path: &[u32]) -> (Option<PyStr>, String);
}

/// The module functions the write gate calls: `_normalize_for_compare`,
/// `_path_exists` and `_absent_by_sparsity`.
pub struct Reads<'a> {
    pub normalize_for_compare: &'a dyn Fn(&[u32]) -> PyStr,
    pub path_exists: &'a dyn Fn(&[u32]) -> bool,
    pub absent_by_sparsity: &'a mut dyn FnMut(&[u32]) -> bool,
}

fn starts(s: &[u32], prefix: &str) -> bool {
    s.len() >= prefix.len()
        && s.iter()
            .zip(prefix.chars())
            .all(|(&a, b)| a == u32::from(b))
}

fn flavour_of(host: &dyn Host) -> Flavour {
    if host.nt() {
        Flavour::Windows
    } else {
        Flavour::Posix
    }
}

/// `_is_absolute_attestation(s)`: a `/` or `~` start, `$HOME/` or
/// `${HOME}/`, or a drive letter (`str.isalpha`, the crate's table), a
/// colon and either slash.
pub fn is_absolute_attestation(s: &[u32]) -> bool {
    if starts(s, "/") || starts(s, "~") {
        return true;
    }
    if starts(s, "$HOME/") || starts(s, "${HOME}/") {
        return true;
    }
    s.len() >= 3
        && unicode::is_alpha(s[0])
        && s[1] == u32::from(b':')
        && (s[2] == SLASH || s[2] == BACKSLASH)
}

/// `str(PurePath(text))` under `flavour`. The POSIX form is written over
/// code points; the Windows form is `config::pure_path`, with each lone
/// surrogate carried through it as an unused private-use code point. A
/// text that holds both stand-in ranges declines ([`Raised::NotPorted`]).
pub fn pure_text(flavour: Flavour, text: &[u32]) -> Result<PyStr, Raised> {
    match flavour {
        Flavour::Posix => {
            if text.is_empty() {
                return Ok(vec![u32::from(b'.')]);
            }
            let lead = text.iter().take_while(|&&c| c == SLASH).count();
            let mut out: PyStr = match lead {
                0 => Vec::new(),
                2 => vec![SLASH, SLASH],
                _ => vec![SLASH],
            };
            let parts: Vec<&[u32]> = text
                .split(|&c| c == SLASH)
                .filter(|p| !p.is_empty() && *p != [u32::from(b'.')])
                .collect();
            for (i, p) in parts.iter().enumerate() {
                if i > 0 {
                    out.push(SLASH);
                }
                out.extend_from_slice(p);
            }
            if out.is_empty() {
                out.push(u32::from(b'.'));
            }
            Ok(out)
        }
        Flavour::Windows => {
            // `config::pure_path` takes text and reads only separators,
            // colons, dots and the characters `?`; a lone surrogate is
            // opaque to it. Each one stands in as a private-use code point
            // the text does not hold, and is mapped back afterwards.
            const SURROGATES: std::ops::RangeInclusive<u32> = 0xD800..=0xDFFF;
            let offset = [0xF_0000 - 0xD800, 0x10_0000 - 0xD800]
                .into_iter()
                .find(|off| {
                    text.iter()
                        .all(|&c| !(0xD800 + off..=0xDFFF + off).contains(&c))
                })
                .ok_or(Raised::NotPorted)?;
            let s: String = text
                .iter()
                .map(|&c| {
                    let c = if SURROGATES.contains(&c) {
                        c + offset
                    } else {
                        c
                    };
                    char::from_u32(c)
                })
                .collect::<Option<String>>()
                .ok_or(Raised::NotPorted)?;
            Ok(pure_path(&s, crate::config::Flavour::Windows)
                .chars()
                .map(|ch| {
                    let c = u32::from(ch);
                    if (0xD800 + offset..=0xDFFF + offset).contains(&c) {
                        c - offset
                    } else {
                        c
                    }
                })
                .collect())
        }
    }
}

/// `_join_under(root, rel)`: None when `rel` escapes; else
/// `_normalize_candidate(str(Path(root) / rel))`, its IndexError raised.
pub fn join_under(flavour: Flavour, root: &[u32], rel: &[u32]) -> Result<Option<PyStr>, Raised> {
    if escapes_root(rel) {
        return Ok(None);
    }
    let joined = pure_text(flavour, &join(flavour, root, &[rel]))?;
    normalize_candidate(&joined).map_err(|_| Raised::Index)
}

fn real(fs: &dyn RealFs, path: &[u8], cwd: &[u8]) -> Result<Option<Vec<u8>>, Raised> {
    match fs.real(path, cwd) {
        Ok(p) => Ok(Some(p)),
        Err(Unresolved::NotPorted) => Err(Raised::NotPorted),
        Err(Unresolved::Raised(_)) => Ok(None),
    }
}

/// A file system's `realpath` and `isdir`, object safe.
pub trait RealFs {
    fn real(&self, path: &[u8], cwd: &[u8]) -> Result<Vec<u8>, Unresolved>;
    fn isdir(&self, path: &[u8]) -> bool;
    fn flavour(&self) -> Flavour;
}

/// An [`Fs`] read under a flavour.
pub struct FlavouredFs<'a, F: Fs + ?Sized> {
    pub fs: &'a F,
    pub flavour: Flavour,
}

impl<F: Fs + ?Sized> RealFs for FlavouredFs<'_, F> {
    fn real(&self, path: &[u8], cwd: &[u8]) -> Result<Vec<u8>, Unresolved> {
        realpath(self.fs, self.flavour, path, cwd)
    }

    /// `os.path.isdir`: `os.stat` follows links; any error is False.
    fn isdir(&self, path: &[u8]) -> bool {
        matches!(self.fs.stat(path), Ok(st) if st.kind == Kind::Directory)
    }

    fn flavour(&self) -> Flavour {
        self.flavour
    }
}

/// `_resolves_under(path, root)` with `os.getcwd()` answering `cwd`:
/// `expanduser` (raised through), `isabs`, then both sides resolved
/// non-strictly and compared by parts. Every OSError and ValueError of the
/// resolution (an unencodable code point, a NUL) is False; the Windows
/// flavour declines ([`Raised::NotPorted`]).
pub fn resolves_under(
    host: &dyn Host,
    fs: &dyn RealFs,
    cwd: &[u8],
    path: &[u32],
    root: &[u32],
) -> Result<bool, Raised> {
    let flavour = fs.flavour();
    let expanded = host.expanduser(path)?;
    if !isabs(flavour, &expanded) {
        return Ok(false);
    }
    if flavour == Flavour::Windows {
        return Err(Raised::NotPorted);
    }
    let Some(cand) = fsencode(&pure_text(flavour, &expanded)?) else {
        return Ok(false);
    };
    let Some(cand) = real(fs, &cand, cwd)? else {
        return Ok(false);
    };
    let Some(base) = fsencode(&pure_text(flavour, root)?) else {
        return Ok(false);
    };
    let Some(base) = real(fs, &base, cwd)? else {
        return Ok(false);
    };
    Ok(cand == base
        || (cand.starts_with(&base)
            && (base.ends_with(b"/") || cand.get(base.len()) == Some(&b'/'))))
}

/// `_worktree_root_is_live(worktree_root)`: `os.path.isdir` of the
/// expanded root (`str(worktree_root)`, as given). An OSError or
/// ValueError of `expanduser` is False; anything else it raises is
/// raised. Under Windows a lone surrogate declines.
pub fn worktree_root_is_live(
    host: &dyn Host,
    fs: &dyn RealFs,
    worktree_root: &[u32],
) -> Result<bool, Raised> {
    let expanded = match host.expanduser(worktree_root) {
        Ok(e) => e,
        Err(Raised::Os(_) | Raised::Value(_)) => return Ok(false),
        Err(e) => return Err(e),
    };
    let bytes = match fs.flavour() {
        Flavour::Posix => fsencode(&expanded),
        Flavour::Windows => Some(
            expanded
                .iter()
                .map(|&c| char::from_u32(c))
                .collect::<Option<String>>()
                .ok_or(Raised::NotPorted)?
                .into_bytes(),
        ),
    };
    Ok(bytes.is_some_and(|b| !b.contains(&0) && fs.isdir(&b)))
}

fn spelled(paths: &Paths, raw: &[u32]) -> PyStr {
    if path_mode(paths) != Mode::Reference {
        return raw.to_vec();
    }
    let start = raw.iter().position(|&c| !unicode::is_space(c));
    match start {
        None => Vec::new(),
        Some(s) => {
            let e = raw
                .iter()
                .rposition(|&c| !unicode::is_space(c))
                .unwrap_or(s);
            raw[s..=e].to_vec()
        }
    }
}

/// `unverifiable_attestations(verified_paths, worktree_root=,
/// anchor=, stored=)`: the offending paths in input order. With an
/// anchor, [`unverifiable_at`]. `worktree_root` is read as
/// `str(Path(worktree_root))` under the host's flavour.
pub fn unverifiable_attestations(
    host: &dyn Host,
    verified_paths: &Paths,
    worktree_root: Option<&[u32]>,
    anchor: Option<&dyn AnchorView>,
    stored: bool,
    reads: &mut Reads<'_>,
) -> Result<Vec<PyStr>, Raised> {
    if let Some(anchor) = anchor {
        return unverifiable_at(host, verified_paths, anchor, stored, reads);
    }
    // `Path(worktree_root)` never raises; the root is spelled at the first
    // non-empty entry, where [`pure_text`] can decline only on a text that
    // holds both of its stand-in ranges.
    let mut root: Option<Option<PyStr>> = None;
    let mode = path_mode(verified_paths);
    let mut bad = Vec::new();
    for raw in &verified_paths.items {
        let stripped = spelled(verified_paths, raw);
        if stripped.is_empty() {
            continue;
        }
        if root.is_none() {
            root = Some(match worktree_root {
                Some(r) => Some(pure_text(flavour_of(host), r)?),
                None => None,
            });
        }
        let root = root.as_ref().and_then(Option::as_ref);
        if placeholder_spelling(host, &stripped, mode, root.map(Vec::as_slice))? {
            bad.push(stripped);
            continue;
        }
        let resolved = if is_absolute_attestation(&stripped) {
            interpret_path(host, &stripped, mode, None)?
        } else if let Some(root) = root {
            joined_path(host, root, &stripped, mode)?
        } else {
            continue;
        };
        let Some(resolved) = resolved else { continue };
        if !(reads.path_exists)(&(reads.normalize_for_compare)(&resolved)) {
            bad.push(resolved);
        }
    }
    Ok(bad)
}

/// `_unverifiable_at(verified_paths, anchor, stored=)`.
pub fn unverifiable_at(
    host: &dyn Host,
    verified_paths: &Paths,
    anchor: &dyn AnchorView,
    stored: bool,
    reads: &mut Reads<'_>,
) -> Result<Vec<PyStr>, Raised> {
    let mode = path_mode(verified_paths);
    let mut bad = Vec::new();
    for raw in &verified_paths.items {
        let spelled = spelled(verified_paths, raw);
        if spelled.is_empty() {
            continue;
        }
        let root = anchor.root();
        if placeholder_spelling(host, &spelled, mode, root.as_deref())? {
            bad.push(spelled);
            continue;
        }
        let (target, refuses) = if is_absolute_attestation(&spelled) {
            let Some(normalized) = interpret_path(host, &spelled, mode, None)? else {
                continue;
            };
            if stored {
                let (target, rule) = anchor.absolute(&normalized);
                let Some(target) = target else { continue };
                (target, CONVICTING_ANCHORS.contains(&rule.as_str()))
            } else {
                (normalized, true)
            }
        } else {
            let Some(root) = root else { continue };
            let Some(joined) = joined_path(host, &root, &spelled, mode)? else {
                continue;
            };
            let refuses = anchor.convicting() || (!stored && anchor.declared_root().is_some());
            (joined, refuses)
        };
        if (reads.path_exists)(&(reads.normalize_for_compare)(&target)) {
            continue;
        }
        if refuses && !(reads.absent_by_sparsity)(&target) {
            bad.push(target);
        }
    }
    Ok(bad)
}

/// `_normalize_attestations(paths, keys)`: the claim keys of the entries
/// that interpret to an absolute attestation, the empty key dropped.
pub fn normalize_attestations(
    host: &dyn Host,
    paths: &Paths,
    keys: &dyn Fn(&[u32]) -> PyStr,
) -> Result<BTreeSet<PyStr>, Raised> {
    let mode = path_mode(paths);
    let mut out = BTreeSet::new();
    for raw in &paths.items {
        if raw.is_empty() {
            continue;
        }
        let Some(candidate) = interpret_path(host, raw, mode, None)? else {
            continue;
        };
        if !is_absolute_attestation(&candidate) {
            continue;
        }
        out.insert(keys(&candidate));
    }
    out.remove(&PyStr::new());
    Ok(out)
}
