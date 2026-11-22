//! `symbols.py`'s checker, its first unit (symcheck): the bounds, the
//! citation and report records, `check_symbol_citations` over citations
//! already parsed, `_tier`, `_live_root`, `_resolve_module` and
//! `_module_bindings`.
//!
//! The parser of citations (`extract_symbol_citations`, with the patterns
//! `_SYMBOL_IN_MODULE` and `_SUPPRESSING_PROSE`) waits for the regular
//! expression engine, so the checker takes the citations as an argument.
//! The module's names come from `ast.parse` and `_top_level_names` /
//! `_all_names`, which wait for a Python parser: they reach the checker
//! through [`ModuleNames`]. The file system is [`Fs`]; `Path.resolve`
//! is `fsdata::realpath` with the working directory an argument.
//!
//! Text is `&[u32]` ([`PyStr`]): a citation may hold a lone surrogate.
//! A path is encoded as `os.fsencode` encodes it (UTF-8, U+DC80 to
//! U+DCFF as the byte they escape); a code point that does not encode is
//! written as a NUL, since both raise ValueError at the first `lstat` of
//! the component that holds it, which every caller here reads as None.

use std::collections::{BTreeMap, BTreeSet};

use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{Flavour, Fs, Kind};
use crate::pyvalue::PyStr;

/// `_MAX_CITATIONS_PER_BODY`.
pub const MAX_CITATIONS_PER_BODY: usize = 8;
/// `_MAX_MODULE_PATH_LEN`.
pub const MAX_MODULE_PATH_LEN: usize = 256;
/// `_MAX_SYMBOL_LEN`.
pub const MAX_SYMBOL_LEN: usize = 96;
/// `_MAX_BODY_SCAN_CHARS`.
pub const MAX_BODY_SCAN_CHARS: usize = 32 * 1024;
/// `_MAX_MODULE_BYTES`: a module larger than this is not read.
pub const MAX_MODULE_BYTES: u64 = 2 * 1024 * 1024;
/// `_FILE_SUFFIXES`, sorted.
pub const FILE_SUFFIXES: [&str; 11] = [
    "cfg", "ini", "json", "jsonl", "md", "py", "sh", "toml", "txt", "yaml", "yml",
];

/// A citation's `binding`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Binding {
    TopLevel,
    Nested,
    Absent,
    Unresolved,
    Unconfirmed,
}

impl Binding {
    /// The reference's string.
    pub fn as_str(self) -> &'static str {
        match self {
            Binding::TopLevel => "top_level",
            Binding::Nested => "nested",
            Binding::Absent => "absent",
            Binding::Unresolved => "unresolved",
            Binding::Unconfirmed => "unconfirmed",
        }
    }
}

/// `SymbolCitation`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SymbolCitation {
    pub symbol: PyStr,
    pub module: PyStr,
    pub binding: Binding,
}

impl SymbolCitation {
    /// `__str__`: `"{symbol} in {module}"`.
    pub fn text(&self) -> PyStr {
        let mut out = self.symbol.clone();
        out.extend(" in ".chars().map(u32::from));
        out.extend_from_slice(&self.module);
        out
    }
}

/// `SymbolDriftReport`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SymbolDriftReport {
    pub checked: Vec<SymbolCitation>,
    pub unresolved: Vec<SymbolCitation>,
    pub unconfirmed: Vec<SymbolCitation>,
}

impl SymbolDriftReport {
    /// `missing`: the checked citations bound nowhere.
    pub fn missing(&self) -> Vec<&SymbolCitation> {
        self.checked
            .iter()
            .filter(|c| c.binding == Binding::Absent)
            .collect()
    }

    /// `__bool__`.
    pub fn truthy(&self) -> bool {
        !(self.checked.is_empty() && self.unresolved.is_empty() && self.unconfirmed.is_empty())
    }

    /// `to_dict()`, its keys in insertion order (`unconfirmed` only when
    /// there is one).
    pub fn to_dict(&self) -> Vec<(&'static str, Vec<PyStr>)> {
        let texts = |list: &[SymbolCitation]| list.iter().map(SymbolCitation::text).collect();
        let mut out = vec![
            ("checked", texts(&self.checked)),
            (
                "missing",
                self.missing()
                    .into_iter()
                    .map(SymbolCitation::text)
                    .collect(),
            ),
            ("unresolved", texts(&self.unresolved)),
        ];
        if !self.unconfirmed.is_empty() {
            out.push(("unconfirmed", texts(&self.unconfirmed)));
        }
        out
    }
}

/// A module's `(top_level, anywhere)` name sets.
pub type NameSets = (BTreeSet<PyStr>, BTreeSet<PyStr>);

/// `ast.parse(source)` and then `_top_level_names` and `_all_names`: None
/// where `ast.parse` raises SyntaxError, ValueError or RecursionError.
pub trait ModuleNames {
    fn names(&self, source: &[u32]) -> Option<NameSets>;
}

/// What the checker reads of a `ClaimAnchor`: its `root` and whether it
/// convicts.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Anchor {
    pub root: Option<PyStr>,
    pub convicting: bool,
}

/// The machine the checker reads: the file system, its flavour and the
/// working directory (`os.getcwd()`, absolute and free of links).
pub struct Machine<'a, F: Fs + ?Sized> {
    pub fs: &'a F,
    pub flavour: Flavour,
    pub cwd: &'a [u8],
}

/// Why the port gives no answer: the Windows flavour's `realpath` is not
/// ported.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Declined {
    NotPorted,
}

/// `check_symbol_citations(body, worktree_root=..., anchor=...)` where
/// `extract_symbol_citations(body)` answered `citations`.
pub fn check_symbol_citations<F: Fs + ?Sized, N: ModuleNames + ?Sized>(
    citations: &[(PyStr, PyStr)],
    worktree_root: Option<&[u32]>,
    anchor: Option<&Anchor>,
    machine: &Machine<'_, F>,
    names: &N,
) -> Result<SymbolDriftReport, Declined> {
    if citations.is_empty() {
        return Ok(SymbolDriftReport::default());
    }
    let (root, convicting) = match anchor {
        Some(anchor) => (
            live_root(anchor.root.as_deref(), machine)?,
            anchor.convicting,
        ),
        None => (live_root(worktree_root, machine)?, true),
    };
    let Some(root) = root else {
        return Ok(SymbolDriftReport::default());
    };
    let mut report = SymbolDriftReport::default();
    let mut cache: BTreeMap<&[u32], Option<NameSets>> = BTreeMap::new();
    for (symbol, module) in citations {
        if !cache.contains_key(module.as_slice()) {
            let index = module_bindings(&root, module, machine, names)?;
            cache.insert(module, index);
        }
        let citation = |binding| SymbolCitation {
            symbol: symbol.clone(),
            module: module.clone(),
            binding,
        };
        let Some((top, anywhere)) = &cache[module.as_slice()] else {
            report.unresolved.push(citation(Binding::Unresolved));
            continue;
        };
        let tier = tier(symbol, top, anywhere);
        if tier == Binding::Absent && !convicting {
            report.unconfirmed.push(citation(Binding::Unconfirmed));
            continue;
        }
        report.checked.push(citation(tier));
    }
    Ok(report)
}

/// `_tier`: a dotted name resolves off either end.
pub fn tier(symbol: &[u32], top_level: &BTreeSet<PyStr>, anywhere: &BTreeSet<PyStr>) -> Binding {
    let dot = u32::from('.');
    let first = symbol.split(|&c| c == dot).next().unwrap_or(&[]);
    let last = symbol.rsplit(|&c| c == dot).next().unwrap_or(&[]);
    if top_level.contains(first) || top_level.contains(last) {
        return Binding::TopLevel;
    }
    if anywhere.contains(first) || anywhere.contains(last) {
        return Binding::Nested;
    }
    Binding::Absent
}

/// `os.fsencode`, with a code point that does not encode written as a
/// NUL (both raise ValueError where the path is stated).
fn fsencode(text: &[u32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(text.len());
    for &cp in text {
        match char::from_u32(cp) {
            Some(c) => {
                let mut buf = [0u8; 4];
                out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            }
            None if (0xDC80..=0xDCFF).contains(&cp) => out.push((cp - 0xDC00) as u8),
            None => out.push(0),
        }
    }
    out
}

/// `Path(path).resolve(strict=False)`: None where it raised ValueError.
fn resolve<F: Fs + ?Sized>(
    path: &[u8],
    machine: &Machine<'_, F>,
) -> Result<Option<Vec<u8>>, Declined> {
    match realpath(machine.fs, machine.flavour, path, machine.cwd) {
        Ok(resolved) => Ok(Some(resolved)),
        Err(Unresolved::Raised(_)) => Ok(None),
        Err(Unresolved::NotPorted) => Err(Declined::NotPorted),
    }
}

/// Whether `os.stat(path)` answers this kind; any error is no.
fn is_kind<F: Fs + ?Sized>(path: &[u8], kind: Kind, machine: &Machine<'_, F>) -> bool {
    machine.fs.stat(path).is_ok_and(|st| st.kind == kind)
}

/// `_live_root`: the root resolved, when it is a directory.
pub fn live_root<F: Fs + ?Sized>(
    worktree_root: Option<&[u32]>,
    machine: &Machine<'_, F>,
) -> Result<Option<Vec<u8>>, Declined> {
    let Some(root) = worktree_root else {
        return Ok(None);
    };
    let Some(root) = resolve(&fsencode(root), machine)? else {
        return Ok(None);
    };
    Ok(is_kind(&root, Kind::Directory, machine).then_some(root))
}

/// `_resolve_module`: the module resolved under `root` (a resolved
/// directory), when it is a regular file inside it.
pub fn resolve_module<F: Fs + ?Sized>(
    root: &[u8],
    module: &[u32],
    machine: &Machine<'_, F>,
) -> Result<Option<Vec<u8>>, Declined> {
    let (slash, backslash, tilde, colon) = (
        u32::from('/'),
        u32::from('\\'),
        u32::from('~'),
        u32::from(':'),
    );
    let start = module.iter().position(|&c| c != slash);
    let stripped = match start {
        None => &module[..0],
        Some(start) => {
            let end = module
                .iter()
                .rposition(|&c| c != slash)
                .map_or(0, |e| e + 1);
            &module[start..end]
        }
    };
    if !stripped.contains(&slash) {
        return Ok(None);
    }
    if matches!(module.first(), Some(&c) if c == slash || c == tilde || c == backslash)
        || module.contains(&backslash)
    {
        return Ok(None);
    }
    if module.len() > 1 && module[1] == colon {
        return Ok(None);
    }
    let dot = u32::from('.');
    if module
        .split(|&c| c == slash)
        .any(|part| part.is_empty() || part == [dot] || part == [dot, dot])
    {
        return Ok(None);
    }
    let mut joined = root.to_vec();
    if joined.last() != Some(&b'/') {
        joined.push(b'/');
    }
    joined.extend_from_slice(&fsencode(module));
    let Some(target) = resolve(&joined, machine)? else {
        return Ok(None);
    };
    if !is_relative_to(&target, root) {
        return Ok(None);
    }
    Ok(is_kind(&target, Kind::Regular, machine).then_some(target))
}

/// `PurePosixPath(target).is_relative_to(root)` of two resolved paths: a
/// component prefix.
fn is_relative_to(target: &[u8], root: &[u8]) -> bool {
    if target == root {
        return true;
    }
    if root == b"/" {
        return target.starts_with(b"/");
    }
    target.len() > root.len() && target.starts_with(root) && target[root.len()] == b'/'
}

/// `path.read_text(encoding="utf-8", errors="replace")`: the bytes
/// decoded with each bad sequence a U+FFFD, and the universal newlines
/// of text mode (`\r\n` and `\r` read as `\n`).
fn read_text<F: Fs + ?Sized>(path: &[u8], machine: &Machine<'_, F>) -> Option<Vec<u32>> {
    let mut file = machine.fs.open(path).ok()?;
    let mut data = Vec::new();
    loop {
        let chunk = machine.fs.read(&mut file, 1 << 16).ok()?;
        if chunk.is_empty() {
            break;
        }
        data.extend_from_slice(&chunk);
    }
    let text = String::from_utf8_lossy(&data);
    let mut out = Vec::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\r' {
            if chars.peek() == Some(&'\n') {
                chars.next();
            }
            out.push(u32::from('\n'));
        } else {
            out.push(u32::from(c));
        }
    }
    Some(out)
}

/// `_module_bindings`: the module's name sets, or None where it does not
/// resolve, is larger than [`MAX_MODULE_BYTES`], cannot be read, or does
/// not parse.
pub fn module_bindings<F: Fs + ?Sized, N: ModuleNames + ?Sized>(
    root: &[u8],
    module: &[u32],
    machine: &Machine<'_, F>,
    names: &N,
) -> Result<Option<NameSets>, Declined> {
    let Some(path) = resolve_module(root, module, machine)? else {
        return Ok(None);
    };
    match machine.fs.stat(&path) {
        Ok(st) if st.size <= MAX_MODULE_BYTES => {}
        _ => return Ok(None),
    }
    Ok(read_text(&path, machine).and_then(|source| names.names(&source)))
}
