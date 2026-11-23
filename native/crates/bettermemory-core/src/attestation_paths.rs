//! `attestation_paths.py` whole: explicit path attestations compiled
//! without the prose grammar, and the legacy reference grammar beside them.
//!
//! The host is an argument ([`Host`]): `os.name == "nt"`, which also picks
//! the flavour of `os.path`; `os.path.expanduser`; and `os.lstat`, of
//! which only whether it raised, and what, is read. [`FsHost`] asks a
//! [`Fs`]. `posixpath` and `ntpath` are the crate's text helpers;
//! `ntpath.splitdrive` and both `commonpath`s are written here. Under a
//! Windows host `ntpath.normcase` asks `LCMapStringEx`, which is not
//! ported: [`Host::nt_normcase`] declines there by default
//! ([`Raised::NotPorted`]).

use std::collections::HashMap;

use crate::fsdata::{Flavour, Fs, PyErr};
use crate::gitfile_rule::hostpath::splitroot;
use crate::gitfile_rule::hostpath::text::{join, normcase, normpath};
use crate::pyvalue::PyStr;
use crate::unicode;
use crate::verify_candidates::validate::{
    escapes_root, is_placeholder_path, normalize_candidate, refused_spelling, trim_candidate,
};
use crate::verify_candidates::MAX_PATH_LENGTH;

/// `_REFERENCE_ROOT`.
pub const REFERENCE_ROOT: &str = "/__attested_reference__";

/// `PathMode`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Auto,
    Literal,
    Reference,
}

impl Mode {
    /// `compile_targets`' check of its `mode` argument.
    pub fn parse(name: &str) -> Result<Mode, Raised> {
        match name {
            "auto" => Ok(Mode::Auto),
            "literal" => Ok(Mode::Literal),
            "reference" => Ok(Mode::Reference),
            _ => Err(Raised::Value(cps(
                "path_mode must be auto, literal or reference",
            ))),
        }
    }
}

/// What a sequence of paths is: `LiteralPaths`, `ReferencePaths`, or a
/// plain sequence.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Literal,
    Reference,
    Plain,
}

/// A sequence of path texts and its class.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Paths<T = PyStr> {
    pub kind: Kind,
    pub items: Vec<T>,
}

/// References to the path strings already held by a record.
pub type PathRefs<'a> = Paths<&'a [u32]>;

/// What a call raises.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Raised {
    /// `IndexError` from `_normalize_candidate`; no clause here catches it.
    Index,
    /// `ValueError`, with its message where the code here words it.
    Value(PyStr),
    /// An `OSError` (or a `RuntimeError`) of the host's call.
    Os(PyErr),
    /// Any other exception of the host's call, by class name; not caught.
    Other(String),
    /// The reference asks what is not ported; not caught.
    NotPorted,
}

impl Raised {
    /// Caught by `except (OSError, ValueError, RuntimeError)`.
    fn caught(&self) -> bool {
        matches!(self, Raised::Value(_) | Raised::Os(_))
    }
}

/// The host the reference asks.
pub trait Host {
    /// `os.name == "nt"`; `os.path` is `ntpath` there.
    fn nt(&self) -> bool;
    /// `os.path.expanduser(path)`.
    fn expanduser(&self, path: &[u32]) -> Result<PyStr, Raised>;
    /// `os.lstat(path)`: Ok, or what it raised.
    fn lstat(&self, path: &[u32]) -> Result<(), Raised>;
    /// `ntpath.normcase(text)`: Python's own off Windows; on a Windows
    /// host it asks `LCMapStringEx`, which is not ported.
    fn nt_normcase(&self, text: &[u32]) -> Result<PyStr, Raised> {
        if self.nt() {
            return Err(Raised::NotPorted);
        }
        Ok(normcase(Flavour::Windows, text))
    }
}

/// A [`Host`] whose `os.lstat` is a file system's.
pub struct FsHost<'a, F: Fs + ?Sized> {
    pub fs: &'a F,
    pub nt: bool,
    pub expanduser: &'a dyn Fn(&[u32]) -> Result<PyStr, Raised>,
}

impl<F: Fs + ?Sized> Host for FsHost<'_, F> {
    fn nt(&self) -> bool {
        self.nt
    }

    fn expanduser(&self, path: &[u32]) -> Result<PyStr, Raised> {
        (self.expanduser)(path)
    }

    /// The path is encoded as `os.fsencode` does on POSIX; a code point
    /// it refuses is the ValueError (UnicodeEncodeError) `os.lstat`
    /// raises. Under Windows a lone surrogate is not ported.
    fn lstat(&self, path: &[u32]) -> Result<(), Raised> {
        let bytes = if self.nt {
            path.iter()
                .map(|&c| char::from_u32(c))
                .collect::<Option<String>>()
                .ok_or(Raised::NotPorted)?
                .into_bytes()
        } else {
            crate::gitfile_rule::follow::fsencode(path).ok_or(Raised::Value(Vec::new()))?
        };
        match self.fs.lstat_result(&bytes) {
            Ok(_) => Ok(()),
            Err(e) if e.class == PyErr::Value => Err(Raised::Value(Vec::new())),
            Err(e) => Err(Raised::Os(e.class)),
        }
    }
}

fn cps(text: &str) -> PyStr {
    text.chars().map(u32::from).collect()
}

fn starts(text: &[u32], prefix: &str) -> bool {
    let p = cps(prefix);
    text.starts_with(&p)
}

fn host_flavour(host: &dyn Host) -> Flavour {
    if host.nt() {
        Flavour::Windows
    } else {
        Flavour::Posix
    }
}

/// `path_mode(paths)`.
pub fn path_mode(paths: &Paths) -> Mode {
    match paths.kind {
        Kind::Literal => Mode::Literal,
        Kind::Reference => Mode::Reference,
        Kind::Plain => Mode::Auto,
    }
}

/// `ntpath.splitdrive(p)`: the drive, and the root with the tail.
pub fn nt_splitdrive(p: &[u32]) -> (PyStr, PyStr) {
    let (drive, mut root, tail) = splitroot(p);
    root.extend(tail);
    (drive, root)
}

/// `posixpath.commonpath` and `ntpath.commonpath` of two paths, None
/// where they raise ValueError (absolute beside relative, two drives).
pub fn commonpath(flavour: Flavour, a: &[u32], b: &[u32]) -> Option<PyStr> {
    let keep = |c: &&[u32]| !c.is_empty() && *c != [u32::from(b'.')];
    match flavour {
        Flavour::Posix => {
            let slash = u32::from(b'/');
            if (a.first() == Some(&slash)) != (b.first() == Some(&slash)) {
                return None;
            }
            let pa: Vec<&[u32]> = a.split(|&c| c == slash).filter(keep).collect();
            let pb: Vec<&[u32]> = b.split(|&c| c == slash).filter(keep).collect();
            let (s1, s2) = if pa <= pb { (pa, pb) } else { (pb, pa) };
            let n = s1.iter().zip(&s2).take_while(|(x, y)| x == y).count();
            let mut out = if a.first() == Some(&slash) {
                vec![slash]
            } else {
                Vec::new()
            };
            out.extend(s1[..n].join(&slash));
            Some(out)
        }
        Flavour::Windows => {
            let sep = u32::from(b'\\');
            let swap = |p: &[u32]| -> PyStr {
                p.iter()
                    .map(|&c| if c == u32::from(b'/') { sep } else { c })
                    .collect()
            };
            let (da, ra, ta) = splitroot(&unicode::lower(&swap(a)));
            let (db, rb, tb) = splitroot(&unicode::lower(&swap(b)));
            if ra != rb || da != db {
                return None;
            }
            let pa: Vec<&[u32]> = ta.split(|&c| c == sep).filter(keep).collect();
            let pb: Vec<&[u32]> = tb.split(|&c| c == sep).filter(keep).collect();
            let (s1, s2) = if pa <= pb { (pa, pb) } else { (pb, pa) };
            let n = s1.iter().zip(&s2).take_while(|(x, y)| x == y).count();
            let (drive, root, path) = splitroot(&swap(a));
            let common: Vec<&[u32]> = path.split(|&c| c == sep).filter(keep).collect();
            let mut out = drive;
            out.extend(root);
            out.extend(common[..n.min(common.len())].join(&sep));
            Some(out)
        }
    }
}

/// `interpret_path(raw, mode=mode, root=root)`.
pub fn interpret_path(
    host: &dyn Host,
    raw: &[u32],
    mode: Mode,
    root: Option<&[u32]>,
) -> Result<Option<PyStr>, Raised> {
    let literal = literal_path(raw);
    if mode == Mode::Literal {
        return Ok(literal);
    }
    if let Some(literal) = literal.as_ref() {
        if mode == Mode::Auto && (root.is_some() || absolute_spelling(literal)) {
            let tried = (|| {
                let impossible = host.nt()
                    && nt_splitdrive(literal)
                        .1
                        .iter()
                        .any(|&c| cps("<>:\"|?*").contains(&c));
                Ok::<bool, Raised>(
                    !impossible && may_be_literal(host, &target_path(host, literal, root)?)?,
                )
            })();
            match tried {
                Ok(true) => return Ok(Some(literal.clone())),
                Ok(false) => {}
                Err(e) if e.caught() => {}
                Err(e) => return Err(e),
            }
        }
    }
    reference_path(raw)
}

/// `_may_be_literal(path)`: `os.lstat` answered, or raised anything but
/// FileNotFoundError and NotADirectoryError. A host call that is not
/// ported ([`Raised::NotPorted`]) is passed up as the decline.
pub fn may_be_literal(host: &dyn Host, path: &[u32]) -> Result<bool, Raised> {
    match host.lstat(path) {
        Err(Raised::NotPorted) => Err(Raised::NotPorted),
        Err(Raised::Os(PyErr::FileNotFound | PyErr::NotADirectory)) => Ok(false),
        _ => Ok(true),
    }
}

/// `joined_path(root, raw, mode=mode)`.
pub fn joined_path(
    host: &dyn Host,
    root: &[u32],
    raw: &[u32],
    mode: Mode,
) -> Result<Option<PyStr>, Raised> {
    let Some(spelled) = interpret_path(host, raw, mode, Some(root))? else {
        return Ok(None);
    };
    match target_path(host, &spelled, Some(root)) {
        Ok(target) => Ok(Some(target)),
        Err(e) if e.caught() => Ok(None),
        Err(e) => Err(e),
    }
}

/// `absolute_spelling(path)`.
pub fn absolute_spelling(path: &[u32]) -> bool {
    ["/", "~", "$HOME/", "${HOME}/"]
        .iter()
        .any(|p| starts(path, p))
        || (path.len() >= 3
            && unicode::is_alpha(path[0])
            && path[1] == u32::from(b':')
            && (path[2] == u32::from(b'/') || path[2] == u32::from(b'\\')))
}

/// `literal_path(raw)`.
pub fn literal_path(raw: &[u32]) -> Option<PyStr> {
    if raw.is_empty() || refused_spelling(raw) {
        return None;
    }
    if !absolute_spelling(raw) && escapes_root(raw) {
        return None;
    }
    for home in ["$HOME/", "${HOME}/"] {
        if starts(raw, home) {
            let mut out = vec![u32::from(b'~')];
            out.extend_from_slice(&raw[home.len() - 1..]);
            return Some(out);
        }
    }
    Some(raw.to_vec())
}

fn strip(text: &[u32]) -> &[u32] {
    let start = text.iter().position(|&c| !unicode::is_space(c));
    let Some(start) = start else { return &[] };
    let end = text
        .iter()
        .rposition(|&c| !unicode::is_space(c))
        .unwrap_or(start);
    &text[start..=end]
}

/// `reference_path(raw)`.
pub fn reference_path(raw: &[u32]) -> Result<Option<PyStr>, Raised> {
    let spelled = strip(raw);
    if spelled.is_empty() {
        return Ok(None);
    }
    if absolute_spelling(spelled) {
        return normalize_candidate(spelled).map_err(|_| Raised::Index);
    }
    if escapes_root(spelled) {
        return Ok(None);
    }
    let prefix = cps(&format!("{REFERENCE_ROOT}/"));
    let mut joined = prefix.clone();
    joined.extend_from_slice(spelled);
    let joined = normalize_candidate(&joined).map_err(|_| Raised::Index)?;
    Ok(joined
        .filter(|j| j.starts_with(&prefix))
        .map(|j| j[prefix.len()..].to_vec()))
}

/// `placeholder_spelling(raw, mode=mode, root=root)`.
pub fn placeholder_spelling(
    host: &dyn Host,
    raw: &[u32],
    mode: Mode,
    root: Option<&[u32]>,
) -> Result<bool, Raised> {
    if is_placeholder_path(strip(raw)) {
        return Ok(true);
    }
    if mode == Mode::Literal || raw.len() > MAX_PATH_LENGTH + 64 {
        return Ok(false);
    }
    let canonical = trim_candidate(raw);
    if !is_placeholder_path(&canonical) {
        return Ok(false);
    }
    if mode == Mode::Reference {
        return Ok(true);
    }
    let Some(literal) = literal_path(raw) else {
        return Ok(true);
    };
    if normpath(Flavour::Posix, &literal) == normpath(Flavour::Posix, &canonical) {
        return Ok(true);
    }
    match target_path(host, &literal, root) {
        Ok(target) => Ok(!may_be_literal(host, &target)?),
        Err(e) if e.caught() => Ok(true),
        Err(e) => Err(e),
    }
}

fn escape_error() -> Raised {
    Raised::Value(cps(
        "a relative path attestation cannot escape its declared root",
    ))
}

/// `target_path(spelled, root)`.
pub fn target_path(
    host: &dyn Host,
    spelled: &[u32],
    root: Option<&[u32]>,
) -> Result<PyStr, Raised> {
    if absolute_spelling(spelled) {
        if starts(spelled, "~") {
            return Ok(normpath(host_flavour(host), &host.expanduser(spelled)?));
        }
        return Ok(spelled.to_vec());
    }
    let Some(root) = root else {
        return Err(Raised::Value(cps(
            "a relative path attestation needs a declared root",
        )));
    };
    if escapes_root(spelled) {
        return Err(escape_error());
    }
    let flavour = if nt_splitdrive(root).0.is_empty() {
        Flavour::Posix
    } else {
        Flavour::Windows
    };
    let base = normpath(flavour, root);
    let target = normpath(flavour, &join(flavour, &base, &[spelled]));
    let inside = commonpath(flavour, &base, &target).unwrap_or_default();
    let case = |text: &[u32]| match flavour {
        Flavour::Posix => Ok(normcase(flavour, text)),
        Flavour::Windows => host.nt_normcase(text),
    };
    if case(&inside)? != case(&base)? {
        return Err(escape_error());
    }
    Ok(target)
}

/// An ordered `dict[str, str | None]`.
pub type Targets = Vec<(PyStr, Option<PyStr>)>;

/// The fields of a stored record the helpers read.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Record {
    pub verified_paths: Vec<PyStr>,
    pub verified_absent_paths: Vec<PyStr>,
    pub verified_path_targets: Option<Targets>,
    pub verified_absent_path_targets: Option<Targets>,
}

fn present(targets: &Targets) -> Vec<PyStr> {
    targets.iter().filter_map(|(_, t)| t.clone()).collect()
}

fn select_paths<'a, T>(
    record: &'a Record,
    absent: bool,
    entry: impl Fn(&'a PyStr) -> T,
) -> Paths<T> {
    let (targets, legacy) = if absent {
        (
            &record.verified_absent_path_targets,
            &record.verified_absent_paths,
        )
    } else {
        (&record.verified_path_targets, &record.verified_paths)
    };
    match targets {
        None => Paths {
            kind: Kind::Reference,
            items: legacy.iter().map(entry).collect(),
        },
        Some(targets) => Paths {
            kind: Kind::Literal,
            items: targets
                .iter()
                .filter_map(|(_, t)| t.as_ref())
                .map(entry)
                .collect(),
        },
    }
}

/// `record_paths(record, absent=absent)` with owned paths.
pub fn record_paths(record: &Record, absent: bool) -> Paths {
    select_paths(record, absent, Clone::clone)
}

/// `record_paths` for a read over the record's lifetime. Python's tuple
/// holds references to the path strings, including each non-null target.
pub fn record_path_refs(record: &Record, absent: bool) -> PathRefs<'_> {
    select_paths(record, absent, Vec::as_slice)
}

/// `commit_paths(record)`.
pub fn commit_paths(record: &Record) -> Result<Paths, Raised> {
    let positive = record_paths(record, false);
    let Some(negative) = &record.verified_absent_path_targets else {
        return Ok(positive);
    };
    let mut items = if positive.kind == Kind::Literal {
        positive.items
    } else {
        let mut out = Vec::new();
        for raw in &positive.items {
            out.extend(reference_path(raw)?);
        }
        out
    };
    items.extend(present(negative));
    Ok(Paths {
        kind: Kind::Literal,
        items,
    })
}

/// `compile_targets(values, root=root, mode=mode, absent=absent)`: the
/// legacy list and the targets, a repeated input keeping its first place
/// and its last value.
pub fn compile_targets(
    host: &dyn Host,
    values: &[PyStr],
    root: Option<&[u32]>,
    mode: Mode,
    absent: bool,
) -> Result<(Vec<PyStr>, Targets), Raised> {
    let mut legacy = Vec::new();
    let mut targets: Targets = Vec::new();
    let mut place: HashMap<PyStr, usize> = HashMap::new();
    for raw in values {
        let mut chosen = if absent && mode != Mode::Reference {
            literal_path(raw)
        } else {
            interpret_path(host, raw, mode, root)?
        };
        if !absent && placeholder_spelling(host, raw, mode, root)? {
            chosen = literal_path(raw);
        }
        if let Some(c) = chosen.as_ref().filter(|c| starts(c, "~")) {
            chosen = Some(normpath(host_flavour(host), &host.expanduser(c)?));
        }
        match place.get(raw) {
            Some(&i) => targets[i].1 = chosen.clone(),
            None => {
                place.insert(raw.clone(), targets.len());
                targets.push((raw.clone(), chosen.clone()));
            }
        }
        let reference = reference_path(raw)?;
        let Some(chosen) = chosen else {
            legacy.push(raw.clone());
            continue;
        };
        if let Some(reference) = reference {
            let pair = target_path(host, &reference, root)
                .and_then(|r| Ok(r == target_path(host, &chosen, root)?));
            let same = match pair {
                Ok(same) => same,
                Err(e) if e.caught() => reference == chosen,
                Err(e) => return Err(e),
            };
            if same {
                legacy.push(raw.clone());
            }
        }
    }
    Ok((legacy, targets))
}

/// `unverifiable_inputs(record, probe)`.
pub fn unverifiable_inputs(
    record: &Record,
    probe: &mut dyn FnMut(&Paths) -> Vec<PyStr>,
) -> Vec<PyStr> {
    let Some(targets) = &record.verified_path_targets else {
        return probe(&Paths {
            kind: Kind::Reference,
            items: record.verified_paths.clone(),
        });
    };
    targets
        .iter()
        .filter(|(_, t)| {
            t.as_ref().is_some_and(|t| {
                !probe(&Paths {
                    kind: Kind::Literal,
                    items: vec![t.clone()],
                })
                .is_empty()
            })
        })
        .map(|(raw, _)| raw.clone())
        .collect()
}
