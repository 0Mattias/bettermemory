//! The pure gates of `handlers/verify.py` (unit h_verify-1): the caps on
//! the `verified_*` lists, `_check_verify_args`, the refusals of a stale
//! stored claim, of a vanished stored attestation and of a stamp that
//! names no evidence, and the path helpers `_relative_to_root`,
//! `_record_root`, `_within` and `_is_callers_tree`.
//!
//! Every effect is an argument. The file system's answers (`Path.resolve`,
//! `os.path.realpath`, `os.path.isdir`, `os.getcwd`) come through
//! [`Machine`]; the checks the reference imports (`load_claims`,
//! `check_claim`, `claim_reason_is_indeterminate`,
//! `unverifiable_attestations`, `detect_path_drift`,
//! `_worktree_root_is_live`, `Anchoring.for_record`) are closures. A
//! `ClaimAnchor` is read for its `root` and `convicting` alone
//! ([`AnchorFacts`]) and a `PathDriftReport` for its three lists
//! ([`DriftLists`]).
//!
//! Text is code points ([`PyStr`]): a path is encoded as `os.fsencode`
//! encodes it, and a code point it cannot encode raises ValueError where
//! the walk states the component holding it, which the port reproduces by
//! stating a NUL byte there. Under [`Flavour::Windows`] `realpath` is not
//! ported, and a gate that must resolve a path answers [`Refused::NotPorted`].

use std::collections::HashSet;

use crate::control_tokens::{refuse_control_tokens, BARE_NAME_REMEDY, FIELD_WHY};
use crate::fsdata::realpath::realpath;
use crate::fsdata::{Flavour, Fs, Kind};
use crate::gitsystem::record::surrogateescape;
use crate::pyvalue::PyStr;
use crate::unicode::cps;

/// `_MAX_VERIFIED_ENTRIES`.
pub const MAX_VERIFIED_ENTRIES: usize = 64;
/// `_MAX_VERIFIED_ITEM_LEN`.
pub const MAX_VERIFIED_ITEM_LEN: usize = 1024;
/// `handlers/_shared.py`'s `_NOTE_MAX_LEN`.
pub const NOTE_MAX_LEN: usize = 800;
/// `_SUGGESTED_PATHS_SHOWN`.
pub const SUGGESTED_PATHS_SHOWN: usize = 8;
/// `_ATTESTATION_REMEDY`.
pub const ATTESTATION_REMEDY: &str = "Leave the entry out.";

/// What a gate answers other than passing.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Refused {
    /// The reference raised ValueError with this message.
    ValueError(PyStr),
    /// The Windows flavour, whose `realpath` is not ported: a decline.
    NotPorted,
}

/// The machine the path helpers ask.
pub struct Machine<'a, F: Fs + ?Sized> {
    pub fs: &'a F,
    pub flavour: Flavour,
    /// `os.getcwd()`, or None where it raises.
    pub cwd: Option<&'a [u8]>,
}

/// The two fields of a `ClaimAnchor` the gates read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AnchorFacts {
    pub root: Option<PyStr>,
    pub convicting: bool,
}

/// The three lists of a `PathDriftReport` the evidence gate reads.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DriftLists {
    pub checked: Vec<PyStr>,
    pub missing: Vec<PyStr>,
    pub expected_absent: Vec<PyStr>,
}

/// How a stored list or a body is read: at an anchor, or at a worktree
/// root (None: relative entries go unchecked).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReadAt<'a> {
    Anchor(&'a AnchorFacts),
    WorktreeRoot(Option<&'a [u32]>),
}

/// An argument whose type the JSON layer checks: absent (None), of the
/// expected type, or of another type (the `isinstance` refusal).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Arg<T> {
    Absent,
    Given(T),
    WrongType,
}

/// Why a resolution gave no path.
enum Unresolved {
    /// OSError or ValueError, which every caller here catches.
    Raised,
    NotPorted,
}

/// `os.path.realpath(path)`, which `Path(path).resolve(strict=False)` is
/// on POSIX (the `Path` spelling drops only what the walk skips).
fn resolve<F: Fs + ?Sized>(m: &Machine<'_, F>, path: &[u32]) -> Result<PyStr, Unresolved> {
    if m.flavour == Flavour::Windows {
        return Err(Unresolved::NotPorted);
    }
    let mut bytes = Vec::with_capacity(path.len());
    for &cp in path {
        if (0xDC80..=0xDCFF).contains(&cp) {
            bytes.push((cp - 0xDC00) as u8);
        } else if let Some(c) = char::from_u32(cp) {
            let mut buf = [0; 4];
            bytes.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
        } else {
            // UnicodeEncodeError where the component is stated, as a NUL is.
            bytes.push(0);
        }
    }
    let cwd: &[u8] = if bytes.starts_with(b"/") {
        b"/"
    } else {
        m.cwd.ok_or(Unresolved::Raised)?
    };
    match realpath(m.fs, m.flavour, &bytes, cwd) {
        Ok(real) => Ok(surrogateescape(&real)),
        Err(crate::fsdata::realpath::Unresolved::NotPorted) => Err(Unresolved::NotPorted),
        Err(crate::fsdata::realpath::Unresolved::Raised(_)) => Err(Unresolved::Raised),
    }
}

/// `os.path.isdir` of a resolved path (always encodable).
fn isdir<F: Fs + ?Sized>(m: &Machine<'_, F>, path: &[u32]) -> bool {
    let Some(bytes) = crate::gitfile_rule::follow::fsencode(path) else {
        return false;
    };
    m.fs.stat(&bytes).is_ok_and(|st| st.kind == Kind::Directory)
}

/// The root `origin_root` resolves to where it is a live directory here;
/// None where it is not; NotPorted under Windows.
fn live_resolved<F: Fs + ?Sized>(
    m: &Machine<'_, F>,
    origin_root: &[u32],
) -> Result<Option<PyStr>, Refused> {
    match resolve(m, origin_root) {
        Ok(root) => Ok(isdir(m, &root).then_some(root)),
        Err(Unresolved::Raised) => Ok(None),
        Err(Unresolved::NotPorted) => Err(Refused::NotPorted),
    }
}

fn s(m: &mut PyStr, text: &str) {
    m.extend(text.chars().map(u32::from));
}

/// `"; ".join(f"{rendered}: {reason}" ...)`.
fn joined(pairs: &[&(PyStr, PyStr)]) -> PyStr {
    let mut out = PyStr::new();
    for (i, (rendered, reason)) in pairs.iter().enumerate() {
        if i > 0 {
            s(&mut out, "; ");
        }
        out.extend_from_slice(rendered);
        s(&mut out, ": ");
        out.extend_from_slice(reason);
    }
    out
}

/// `_refuse_stale_stored_claims(stored, origin_root, anchor=anchor)`.
/// `load_claims` gives the claims, `check_claim(claim, root)` a reason
/// or None (root being `str` of the resolved `Path`), `render` each
/// claim's `render()`, `indeterminate` `claim_reason_is_indeterminate`.
#[allow(clippy::too_many_arguments)]
pub fn refuse_stale_stored_claims<F: Fs + ?Sized, C>(
    m: &Machine<'_, F>,
    stored: &[PyStr],
    origin_root: Option<&[u32]>,
    anchor: Option<&AnchorFacts>,
    load_claims: impl FnOnce(&[PyStr]) -> Vec<C>,
    mut check_claim: impl FnMut(&C, &[u32]) -> Option<PyStr>,
    render: impl Fn(&C) -> PyStr,
    indeterminate: impl Fn(&[u32]) -> bool,
) -> Result<(), Refused> {
    let origin_root = match anchor {
        Some(a) if !a.convicting => return Ok(()),
        Some(a) => a.root.as_deref(),
        None => origin_root,
    };
    let Some(origin_root) = origin_root else {
        return Ok(());
    };
    let Some(root) = live_resolved(m, origin_root)? else {
        return Ok(());
    };
    let mut failures = Vec::new();
    for claim in load_claims(stored) {
        if let Some(reason) = check_claim(&claim, &root) {
            failures.push((render(&claim), reason));
        }
    }
    let contradicted: Vec<_> = failures.iter().filter(|p| !indeterminate(&p.1)).collect();
    let unchecked: Vec<_> = failures.iter().filter(|p| indeterminate(&p.1)).collect();
    let mut msg = PyStr::new();
    if !contradicted.is_empty() {
        s(
            &mut msg,
            &format!(
                "cannot verify: {} stored claim(s) no longer hold \u{2014} ",
                contradicted.len()
            ),
        );
        msg.extend(joined(&contradicted));
        s(
            &mut msg,
            ". A verify stamps the whole record fresh, and its claims are part of the record. \
memory_update the body first (body edits clear claims), or re-declare corrected claims by \
passing claims=[...] on this call.",
        );
        return Err(Refused::ValueError(msg));
    }
    if !unchecked.is_empty() {
        s(
            &mut msg,
            &format!(
                "cannot verify: {} stored claim(s) could not be checked here \u{2014} ",
                unchecked.len()
            ),
        );
        msg.extend(joined(&unchecked));
        s(
            &mut msg,
            ". A verify stamps the whole record fresh, and an unchecked claim is not a checked \
one. Verify from a machine that can read the tree, or re-declare corrected claims by passing \
claims=[...] on this call.",
        );
        return Err(Refused::ValueError(msg));
    }
    Ok(())
}

/// `_refuse_unverifiable_stored_attestations(stored, origin_root,
/// anchor=anchor)`. `unverifiable` is `unverifiable_attestations`, called
/// with `anchor=..., stored=True` ([`ReadAt::Anchor`]) or with
/// `worktree_root=` the ORIGINAL `origin_root` where it resolves to a live
/// directory, else None.
pub fn refuse_unverifiable_stored_attestations<F: Fs + ?Sized>(
    m: &Machine<'_, F>,
    stored: &[PyStr],
    origin_root: Option<&[u32]>,
    anchor: Option<&AnchorFacts>,
    unverifiable: impl FnOnce(&[PyStr], ReadAt<'_>) -> Vec<PyStr>,
) -> Result<(), Refused> {
    if stored.is_empty() {
        return Ok(());
    }
    let unseen = if let Some(anchor) = anchor {
        unverifiable(stored, ReadAt::Anchor(anchor))
    } else {
        let root = match origin_root {
            Some(r) if live_resolved(m, r)?.is_some() => Some(r),
            _ => None,
        };
        unverifiable(stored, ReadAt::WorktreeRoot(root))
    };
    if unseen.is_empty() {
        return Ok(());
    }
    let mut msg = PyStr::new();
    s(
        &mut msg,
        &format!(
            "cannot attest {} stored path(s) that do not exist on this machine: ",
            unseen.len()
        ),
    );
    for (i, p) in unseen.iter().enumerate() {
        if i > 0 {
            s(&mut msg, ", ");
        }
        msg.extend_from_slice(p);
    }
    s(
        &mut msg,
        ". verified_paths=None preserves the stored attestation list, and stamping \
last_verified_at asserts it still holds. Pass a corrected verified_paths list (the lists \
REPLACE, not append), or move intentionally-absent entries to verified_absent_paths.",
    );
    Err(Refused::ValueError(msg))
}

/// `_relative_to_root(path, root)`.
pub fn relative_to_root<F: Fs + ?Sized>(
    m: &Machine<'_, F>,
    path: &[u32],
    root: Option<&[u32]>,
) -> Result<PyStr, Refused> {
    let Some(root) = root else {
        return Ok(path.to_vec());
    };
    let here = match resolve(m, path) {
        Ok(p) => p,
        Err(Unresolved::Raised) => return Ok(path.to_vec()),
        Err(Unresolved::NotPorted) => return Err(Refused::NotPorted),
    };
    let top = match resolve(m, root) {
        Ok(p) => p,
        Err(Unresolved::Raised) => return Ok(path.to_vec()),
        Err(Unresolved::NotPorted) => return Err(Refused::NotPorted),
    };
    // Both are absolute and normalized: `relative_to` compares components.
    let parts = |p: &[u32]| -> Vec<PyStr> {
        p.split(|&c| c == u32::from(b'/'))
            .filter(|c| !c.is_empty())
            .map(<[u32]>::to_vec)
            .collect()
    };
    let (h, t) = (parts(&here), parts(&top));
    if h.len() < t.len() || h[..t.len()] != t[..] {
        return Ok(path.to_vec());
    }
    let rest = &h[t.len()..];
    if rest.is_empty() {
        return Ok(vec![u32::from(b'.')]);
    }
    Ok(rest.join(&u32::from(b'/')))
}

/// `_refuse_evidence_free_stamp(memory_id, body, origin_root, ...)`.
/// `root_is_live` is `_worktree_root_is_live`; `drift` is
/// `detect_path_drift` with `anchor=` or `worktree_root=`.
#[allow(clippy::too_many_arguments, clippy::fn_params_excessive_bools)]
pub fn refuse_evidence_free_stamp<F: Fs + ?Sized>(
    m: &Machine<'_, F>,
    memory_id: &[u32],
    body: &[u32],
    origin_root: Option<&[u32]>,
    attests_paths: bool,
    attests_absence: bool,
    declares_claims: bool,
    anchor: Option<&AnchorFacts>,
    root_is_live: impl FnOnce(&[u32]) -> bool,
    drift: impl FnOnce(&[u32], ReadAt<'_>) -> DriftLists,
) -> Result<(), Refused> {
    if attests_paths || attests_absence || declares_claims {
        return Ok(());
    }
    let (root, report) = if let Some(anchor) = anchor {
        (anchor.root.as_deref(), drift(body, ReadAt::Anchor(anchor)))
    } else {
        let root = origin_root.filter(|r| root_is_live(r));
        (root, drift(body, ReadAt::WorktreeRoot(root)))
    };
    // `set(report.missing) | set(report.expected_absent)`.
    let gone: HashSet<&PyStr> = report
        .missing
        .iter()
        .chain(&report.expected_absent)
        .collect();
    let resolved: Vec<&PyStr> = report
        .checked
        .iter()
        .filter(|p| !gone.contains(p))
        .collect();
    if resolved.is_empty() {
        return Ok(());
    }
    let mut shown = PyStr::new();
    for (i, p) in resolved.iter().take(SUGGESTED_PATHS_SHOWN).enumerate() {
        if i > 0 {
            s(&mut shown, ", ");
        }
        shown.extend(relative_to_root(m, p, root)?);
    }
    // The reference makes every suggestion, shown or not. The ones past the
    // eighth are not made here: under one flavour a suggestion either
    // declines on the first call (Windows, with a root) or never declines,
    // so the unshown ones cannot change the answer.
    if resolved.len() > SUGGESTED_PATHS_SHOWN {
        s(
            &mut shown,
            &format!(", and {} more", resolved.len() - SUGGESTED_PATHS_SHOWN),
        );
    }
    let mut msg = PyStr::new();
    s(&mut msg, "cannot verify: memory ");
    msg.extend_from_slice(memory_id);
    s(
        &mut msg,
        &format!(
            " cites {} path(s) that resolve here and this call attests none of them: ",
            resolved.len()
        ),
    );
    msg.extend(shown);
    s(
        &mut msg,
        ". A stamp that names nothing it checked asserts nothing the read side can use. Pass \
verified_paths with the ones you looked at, verified_absent_paths for any that should be \
absent, or claims for what the body asserts about them.",
    );
    Err(Refused::ValueError(msg))
}

/// `_record_root(snapshot, anchoring)`: `worktree_root` is the origin's
/// (None for no origin too); `for_record` is `anchoring.for_record`, None
/// for no anchoring.
pub fn record_root(
    worktree_root: Option<&[u32]>,
    record_id: &[u32],
    for_record: Option<impl FnOnce(&[u32]) -> Option<AnchorFacts>>,
) -> Option<PyStr> {
    if let Some(root) = worktree_root {
        return Some(root.to_vec());
    }
    for_record?(record_id)?.root
}

fn real_or<F: Fs + ?Sized>(m: &Machine<'_, F>, path: &[u32]) -> Result<Option<PyStr>, Refused> {
    match resolve(m, path) {
        Ok(p) => Ok(Some(p)),
        Err(Unresolved::Raised) => Ok(None),
        Err(Unresolved::NotPorted) => Err(Refused::NotPorted),
    }
}

/// `_within(path, root)`.
pub fn within<F: Fs + ?Sized>(
    m: &Machine<'_, F>,
    path: Option<&[u32]>,
    root: Option<&[u32]>,
) -> Result<bool, Refused> {
    let (Some(path), Some(root)) = (path, root) else {
        return Ok(false);
    };
    let Some(here) = real_or(m, path)? else {
        return Ok(false);
    };
    let Some(top) = real_or(m, root)? else {
        return Ok(false);
    };
    let slash = u32::from(b'/');
    let mut prefix = top.clone();
    while prefix.last() == Some(&slash) {
        prefix.pop();
    }
    prefix.push(slash);
    Ok(here == top || here.starts_with(&prefix))
}

/// `_is_callers_tree(root, caller_root)`.
pub fn is_callers_tree<F: Fs + ?Sized>(
    m: &Machine<'_, F>,
    root: Option<&[u32]>,
    caller_root: Option<&[u32]>,
) -> Result<bool, Refused> {
    let (Some(root), Some(caller)) = (root, caller_root) else {
        return Ok(false);
    };
    let Some(a) = real_or(m, root)? else {
        return Ok(false);
    };
    let Some(b) = real_or(m, caller)? else {
        return Ok(false);
    };
    Ok(a == b)
}

/// `_check_verify_args(note, verified_paths, verified_commits,
/// verified_versions, verified_absent_paths)`: the lists in that order.
pub fn check_verify_args(note: Arg<&[u32]>, lists: [Arg<&[PyStr]>; 4]) -> Result<(), PyStr> {
    let note = match note {
        Arg::WrongType => return Err(cps("note must be a string if provided")),
        Arg::Absent => None,
        Arg::Given(n) => Some(n),
    };
    if let Some(n) = note.filter(|n| n.len() > NOTE_MAX_LEN) {
        let mut msg = PyStr::new();
        s(
            &mut msg,
            &format!(
                "note is {} chars \u{2014} cap is {NOTE_MAX_LEN}. The note is a short rationale \
for the verification, not a place to paste prose; trim it before recording.",
                n.len()
            ),
        );
        return Err(msg);
    }
    refuse_control_tokens(&cps("note"), note, &cps(BARE_NAME_REMEDY), &cps(FIELD_WHY))
        .map_err(|e| e.message)?;
    let labels = [
        "verified_paths",
        "verified_commits",
        "verified_versions",
        "verified_absent_paths",
    ];
    for (label, value) in labels.iter().zip(lists) {
        let value = match value {
            Arg::Absent => continue,
            Arg::WrongType => {
                return Err(cps(&format!(
                    "{label} must be a list of strings if provided"
                )))
            }
            Arg::Given(v) => v,
        };
        if value.len() > MAX_VERIFIED_ENTRIES {
            return Err(cps(&format!(
                "{label} capped at {MAX_VERIFIED_ENTRIES} entries (got {}); a memory cites a \
handful of paths, not a manifest",
                value.len()
            )));
        }
        for (index, item) in value.iter().enumerate() {
            if item.len() > MAX_VERIFIED_ITEM_LEN {
                return Err(cps(&format!(
                    "{label} entry is {} chars \u{2014} cap is {MAX_VERIFIED_ITEM_LEN}. \
Attestations are short path/commit/version strings, not prose.",
                    item.len()
                )));
            }
            refuse_control_tokens(
                &cps(&format!("{label}[{index}]")),
                Some(item),
                &cps(ATTESTATION_REMEDY),
                &cps(FIELD_WHY),
            )
            .map_err(|e| e.message)?;
        }
    }
    Ok(())
}
