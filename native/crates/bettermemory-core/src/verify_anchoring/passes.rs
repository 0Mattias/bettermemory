//! The attestation and citation read passes of `verify_anchoring.py`
//! (reference lines 1237-1384, 1387-1507 and 1587-1718): the relative
//! attestation keys a body citation defers to, the anchored read of the
//! attestations and of the body's relative citations, the recorded-root
//! passes `_check_anchored_attestations` and `_check_anchored_citations`,
//! and the citation filter `_is_checkable_citation`.
//!
//! Effects are arguments. The host ([`Host`]) answers `os.name`,
//! `os.path.expanduser` and the `os.lstat` of `attestation_paths`; the
//! module functions `_claim_key`, `_path_exists`, `_absent_by_sparsity`,
//! `_resolves_under` and `os.path.isdir` are the methods of
//! [`ClaimReads`]; the anchor is a [`ReadAnchor`]. `_RELATIVE_CITATION_RE`
//! waits for the regular expression engine: its `finditer` over the
//! bounded text is a closure answering the span of group 1 of each match,
//! in order.

use std::collections::HashSet;

use crate::attestation_paths::{interpret_path, joined_path, path_mode, Host, Mode, Paths, Raised};
use crate::fsdata::Flavour;
use crate::gitfile_rule::hostpath::text::dirname;
use crate::pyvalue::PyStr;
use crate::unicode;
use crate::verify_anchoring::anchor::{CONVICTING_ANCHORS, SPARSE_CHECKOUT};
use crate::verify_anchoring::attest::{is_absolute_attestation, join_under, pure_text};
use crate::verify_candidates::validate::{
    bounded_scan_text, escapes_root, is_placeholder_path, refused_spelling,
};
use crate::verify_candidates::{CHECKABLE_CITATION_EXTENSIONS, MAX_ANCHORED_CITATION_STATS};

/// What the passes read of a `ClaimAnchor`: `root`, `rule`, `convicting`
/// and `absolute(path)` (the path to read, or None, and its rule).
pub trait ReadAnchor {
    fn root(&self) -> Option<PyStr>;
    fn rule(&self) -> String;
    fn convicting(&self) -> bool;
    fn absolute(&self, path: &[u32]) -> (Option<PyStr>, String);
}

/// The module functions the passes call.
pub trait ClaimReads {
    /// `_claim_key(path)`, through the read's `keys`.
    fn key(&mut self, path: &[u32]) -> PyStr;
    /// `_path_exists(path)`.
    fn path_exists(&mut self, path: &[u32]) -> bool;
    /// `_absent_by_sparsity(path)`.
    fn absent_by_sparsity(&mut self, path: &[u32]) -> bool;
    /// `_resolves_under(path, Path(root))`, `root` spelled as `str` of it.
    fn resolves_under(&mut self, path: &[u32], root: &[u32]) -> Result<bool, Raised>;
    /// `os.path.isdir(path)`.
    fn isdir(&mut self, path: &[u32]) -> bool;
}

/// `_RELATIVE_CITATION_RE.finditer(text)`: the span of group 1 of each
/// match, in order. A span outside `text` is passed over.
pub type CitationMatches<'a> = &'a dyn Fn(&[u32]) -> Vec<(usize, usize)>;

/// `_AnchoredRead`: the buckets of one anchored read and the seen set
/// every pass dedupes against. `_check_anchored_attestations` and
/// `_check_anchored_citations` take the buckets they write from here.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct AnchoredRead {
    pub checked: Vec<PyStr>,
    pub missing: Vec<PyStr>,
    pub claim_anchored: Vec<PyStr>,
    pub verified: Vec<PyStr>,
    pub expected_absent: Vec<PyStr>,
    pub dropped_as_route: Vec<PyStr>,
    pub unconfirmed: Vec<PyStr>,
    pub reasons: Vec<String>,
    pub present_absent: Vec<PyStr>,
    pub seen: HashSet<PyStr>,
}

impl AnchoredRead {
    /// `first(path)`: mark the key of `path` read; False when it was.
    pub fn first(&mut self, reads: &mut dyn ClaimReads, path: &[u32]) -> bool {
        self.seen.insert(reads.key(path))
    }

    /// `unconfirm(path, reason)`.
    pub fn unconfirm(&mut self, path: &[u32], reason: &str) {
        self.unconfirmed.push(path.to_vec());
        self.reasons.push(reason.to_string());
    }

    /// `miss(path, convicting=, reason=, claim=)`: unconfirmed where the
    /// read does not convict, then where a sparse checkout leaves the path
    /// out; a miss otherwise.
    pub fn miss(
        &mut self,
        reads: &mut dyn ClaimReads,
        path: &[u32],
        convicting: bool,
        reason: &str,
        claim: bool,
    ) {
        if !convicting {
            self.unconfirm(path, reason);
        } else if reads.absent_by_sparsity(path) {
            self.unconfirm(path, SPARSE_CHECKOUT);
        } else {
            self.checked.push(path.to_vec());
            self.missing.push(path.to_vec());
            if claim {
                self.claim_anchored.push(path.to_vec());
            }
        }
    }
}

fn flavour_of(host: &dyn Host) -> Flavour {
    if host.nt() {
        Flavour::Windows
    } else {
        Flavour::Posix
    }
}

/// `_relative_attestation_keys(root, verified_paths, absent_paths, keys)`.
/// `root` is read as given (`str(root)`), not through `Path`.
pub fn relative_attestation_keys(
    host: &dyn Host,
    reads: &mut dyn ClaimReads,
    root: &[u32],
    verified_paths: &Paths,
    absent_paths: &Paths,
) -> Result<HashSet<PyStr>, Raised> {
    let mut out = HashSet::new();
    for paths in [verified_paths, absent_paths] {
        for raw in &paths.items {
            let spelled = interpret_path(host, raw, path_mode(paths), Some(root))?;
            let Some(spelled) = spelled.filter(|s| !s.is_empty()) else {
                continue;
            };
            if is_absolute_attestation(&spelled) {
                continue;
            }
            if let Some(joined) = joined_path(host, root, &spelled, Mode::Literal)? {
                out.insert(reads.key(&joined));
            }
        }
    }
    out.remove(&PyStr::new());
    Ok(out)
}

/// `_AttestationRead`: where one attestation is read, the rule, whether
/// an absence there convicts, and whether it is readable at all.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AttestationRead {
    pub path: PyStr,
    pub rule: String,
    pub convicting: bool,
    pub readable: bool,
}

/// `_attestation_read(anchor, root, raw, mode=)`. `root` is `str` of
/// `Path(anchor.root)`.
pub fn attestation_read(
    host: &dyn Host,
    reads: &mut dyn ClaimReads,
    anchor: &dyn ReadAnchor,
    root: Option<&[u32]>,
    raw: &[u32],
    mode: Mode,
) -> Result<Option<AttestationRead>, Raised> {
    let spelled = interpret_path(host, raw, mode, root)?;
    let Some(spelled) = spelled.filter(|s| !s.is_empty() && !refused_spelling(s)) else {
        return Ok(None);
    };
    let read = |path: PyStr, rule: String, convicting: bool, readable: bool| {
        Ok(Some(AttestationRead {
            path,
            rule,
            convicting,
            readable,
        }))
    };
    if is_absolute_attestation(&spelled) {
        let (target, rule) = anchor.absolute(&spelled);
        let Some(target) = target else {
            return read(spelled, rule, false, false);
        };
        if rule == "absolute" {
            let under = match root {
                None => false,
                Some(root) => reads.resolves_under(&target, root)?,
            };
            if !under {
                return Ok(None);
            }
        }
        let convicting = CONVICTING_ANCHORS.contains(&rule.as_str());
        return read(target, rule, convicting, true);
    }
    if escapes_root(&spelled) {
        return Ok(None);
    }
    let Some(root) = root else {
        return read(spelled, anchor.rule(), false, false);
    };
    match joined_path(host, root, &spelled, Mode::Literal)? {
        None => Ok(None),
        Some(joined) => read(joined, anchor.rule(), anchor.convicting(), true),
    }
}

fn anchor_root(host: &dyn Host, anchor: &dyn ReadAnchor) -> Result<Option<PyStr>, Raised> {
    anchor
        .root()
        .map(|r| pure_text(flavour_of(host), &r))
        .transpose()
}

/// `_read_attestations(anchor, verified_paths, absent_paths, out)`.
pub fn read_attestations(
    host: &dyn Host,
    reads: &mut dyn ClaimReads,
    anchor: &dyn ReadAnchor,
    verified_paths: &Paths,
    absent_paths: &Paths,
    out: &mut AnchoredRead,
) -> Result<(), Raised> {
    let root = anchor_root(host, anchor)?;
    let mut found_reads: Vec<(AttestationRead, bool)> = Vec::new();
    for (listed_absent, paths) in [(false, verified_paths), (true, absent_paths)] {
        for raw in &paths.items {
            let found =
                attestation_read(host, reads, anchor, root.as_deref(), raw, path_mode(paths))?;
            if let Some(found) = found {
                found_reads.push((found, listed_absent));
            }
        }
    }
    let mut absent = HashSet::new();
    for (found, listed) in &found_reads {
        if *listed && found.readable {
            absent.insert(reads.key(&found.path));
        }
    }
    for (found, _listed) in &found_reads {
        if !found.readable {
            if out.first(reads, &found.path) {
                out.unconfirm(&found.path, &found.rule);
            }
            continue;
        }
        let target = &found.path;
        if !out.first(reads, target) {
            continue;
        }
        let is_absent = absent.contains(&reads.key(target));
        if reads.path_exists(target) {
            if !is_absent {
                out.checked.push(target.clone());
                out.verified.push(target.clone());
            } else if found.convicting {
                out.checked.push(target.clone());
                out.present_absent.push(target.clone());
            } else {
                out.unconfirm(target, &found.rule);
            }
        } else if is_absent {
            out.checked.push(target.clone());
            out.expected_absent.push(target.clone());
        } else {
            out.miss(reads, target, found.convicting, &found.rule, true);
        }
    }
    Ok(())
}

/// The parent rule of both citation passes: `os.path.isdir` of
/// `os.path.expanduser(os.path.dirname(resolved))`.
fn parent_is_dir(
    host: &dyn Host,
    reads: &mut dyn ClaimReads,
    resolved: &[u32],
) -> Result<bool, Raised> {
    let parent = host.expanduser(&dirname(flavour_of(host), resolved))?;
    Ok(reads.isdir(&parent))
}

/// `_read_citations(anchor, body, out)`. The anchor has a root (the
/// reference asserts it); one without reads nothing here.
pub fn read_citations(
    host: &dyn Host,
    reads: &mut dyn ClaimReads,
    anchor: &dyn ReadAnchor,
    body: &[u32],
    matches: CitationMatches<'_>,
    out: &mut AnchoredRead,
) -> Result<(), Raised> {
    let Some(root) = anchor_root(host, anchor)? else {
        return Ok(());
    };
    let text = bounded_scan_text(body);
    let mut stats = 0;
    for (start, end) in matches(text) {
        if stats >= MAX_ANCHORED_CITATION_STATS {
            break;
        }
        // A span past the bounded text is no match of it.
        let Some(cited) = text.get(start..end) else {
            continue;
        };
        if !is_checkable_citation(cited) {
            continue;
        }
        let Some(resolved) = join_under(flavour_of(host), &root, cited)? else {
            continue;
        };
        if !out.first(reads, &resolved) {
            continue;
        }
        stats += 1;
        if reads.path_exists(&resolved) {
            out.checked.push(resolved);
            continue;
        }
        if !parent_is_dir(host, reads, &resolved)? {
            continue;
        }
        let (convicting, rule) = (anchor.convicting(), anchor.rule());
        out.miss(reads, &resolved, convicting, &rule, true);
    }
    Ok(())
}

/// `_check_anchored_attestations(worktree_root, verified_paths,
/// absent_paths, ...)` over `out`'s `checked`, `missing`,
/// `claim_anchored`, `verified`, `expected_absent` and `seen`.
pub fn check_anchored_attestations(
    host: &dyn Host,
    reads: &mut dyn ClaimReads,
    worktree_root: Option<&[u32]>,
    verified_paths: &Paths,
    absent_paths: &Paths,
    out: &mut AnchoredRead,
) -> Result<(), Raised> {
    let Some(root) = worktree_root else {
        return Ok(());
    };
    let root = pure_text(flavour_of(host), root)?;
    let anchored =
        |reads: &mut dyn ClaimReads, raw: &[u32], paths: &Paths| -> Result<Option<PyStr>, Raised> {
            let rel = interpret_path(host, raw, path_mode(paths), Some(&root))?;
            let Some(rel) = rel.filter(|r| !r.is_empty()) else {
                return Ok(None);
            };
            if is_absolute_attestation(&rel) {
                if !reads.resolves_under(&rel, &root)? {
                    return Ok(None);
                }
                return Ok(Some(rel));
            }
            joined_path(host, &root, &rel, Mode::Literal)
        };
    let mut absent_set = HashSet::new();
    for raw in &absent_paths.items {
        if let Some(a) = anchored(reads, raw, absent_paths)? {
            absent_set.insert(reads.key(&a));
        }
    }
    for paths in [verified_paths, absent_paths] {
        for raw in &paths.items {
            let Some(resolved) = anchored(reads, raw, paths)? else {
                continue;
            };
            let norm = reads.key(&resolved);
            if out.seen.contains(&norm) {
                continue;
            }
            let held = absent_set.contains(&norm);
            out.seen.insert(norm);
            out.checked.push(resolved.clone());
            if reads.path_exists(&resolved) {
                if !held {
                    out.verified.push(resolved);
                }
            } else if held {
                out.expected_absent.push(resolved);
            } else {
                out.missing.push(resolved.clone());
                out.claim_anchored.push(resolved);
            }
        }
    }
    Ok(())
}

/// `_is_checkable_citation(rel)`: a directory segment, a first segment
/// with no dot past its first code point, a listed extension (by
/// `str.lower`) and no placeholder once rooted.
pub fn is_checkable_citation(rel: &[u32]) -> bool {
    let slash = u32::from(b'/');
    let dot = u32::from(b'.');
    let Some(cut) = rel.iter().position(|&c| c == slash) else {
        return false;
    };
    if rel[..cut].iter().skip(1).any(|&c| c == dot) {
        return false;
    }
    let ext_start = rel.iter().rposition(|&c| c == dot).map_or(0, |i| i + 1);
    let ext = unicode::lower(&rel[ext_start..]);
    let listed = CHECKABLE_CITATION_EXTENSIONS
        .iter()
        .any(|e| e.chars().map(u32::from).eq(ext.iter().copied()));
    if !listed {
        return false;
    }
    let mut rooted = vec![slash];
    rooted.extend_from_slice(rel);
    !is_placeholder_path(&rooted)
}

/// `_check_anchored_citations(root, body, ...)` over `out`'s `checked`,
/// `missing`, `claim_anchored` and `seen`. `root` is `str` of the
/// caller's `Path`.
pub fn check_anchored_citations(
    host: &dyn Host,
    reads: &mut dyn ClaimReads,
    root: &[u32],
    body: &[u32],
    matches: CitationMatches<'_>,
    out: &mut AnchoredRead,
) -> Result<(), Raised> {
    let flavour = flavour_of(host);
    let text = bounded_scan_text(body);
    let mut stats = 0;
    for (start, end) in matches(text) {
        if stats >= MAX_ANCHORED_CITATION_STATS {
            break;
        }
        // A span past the bounded text is no match of it.
        let Some(cited) = text.get(start..end) else {
            continue;
        };
        if !is_checkable_citation(cited) {
            continue;
        }
        let Some(resolved) = join_under(flavour, root, cited)? else {
            continue;
        };
        if !out.first(reads, &resolved) {
            continue;
        }
        stats += 1;
        if reads.path_exists(&resolved) {
            out.checked.push(resolved);
            continue;
        }
        if !parent_is_dir(host, reads, &resolved)? {
            continue;
        }
        out.checked.push(resolved.clone());
        out.missing.push(resolved.clone());
        out.claim_anchored.push(resolved);
    }
    Ok(())
}
