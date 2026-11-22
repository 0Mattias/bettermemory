//! `os.path.realpath` and `os.path.abspath` over a file system ([`Fs`]),
//! with the working directory as an input, never read from the process.
//!
//! On POSIX both are CPython 3.13's `posixpath` (3.13.13): `realpath`
//! (non-strict) walks the path a component at a time with `lstat` and
//! `readlink`, ignores every OSError (the path so far stands), lets a
//! ValueError (a NUL) through, and stops at a link it is still resolving
//! (a loop) with the path as reached. The walk keeps its own stack, so no
//! link recurses: a chain of any length costs memory, not stack.
//!
//! `realpath_strict` is the same walk with 3.13's `strict=True`: each
//! error raised with its errno's name.
//!
//! Over an [`Fs`] alone the Windows flavour answers
//! [`Unresolved::NotPorted`], which a caller reads as a decline: the
//! Windows calls are not the file-system trait's. They are [`FinalPath`]'s,
//! and over it [`nt_realpath`] is CPython 3.13.13's `ntpath.realpath`
//! (non-strict, `Lib/ntpath.py` 563-731: `_readlink_deep`,
//! `_getfinalpathname_nonstrict` and `realpath`) and [`nt_abspath`] its
//! `abspath` over `_getfullpathname` (529-554, through
//! `gitfile_rule::hostpath::text::abspath`). 3.13.13 is the interpreter
//! the contract generators run under (`.python-version`). Paths there
//! are Python `str` (code points), and the module's Python spellings of
//! `normpath`, `splitroot` and `normcase` are the ones reproduced (on a
//! Windows host `normcase` asks `LCMapStringEx`, which agrees on ASCII).

use std::collections::HashMap;

use super::{Flavour, Fs, Kind, OsError, PyErr};
use crate::gitfile_rule::hostpath::text::{self, Process};
use crate::pyvalue::PyStr;
use crate::unicode::cps;

/// Why a path was not resolved.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Unresolved {
    /// The reference raised this (ValueError: a NUL in a component it
    /// stated).
    Raised(PyErr),
    /// The Windows flavour, which is not ported.
    NotPorted,
}

/// `posixpath.normpath`.
fn normpath(path: &[u8]) -> Vec<u8> {
    if path.is_empty() {
        return b".".to_vec();
    }
    let mut initial = usize::from(path.starts_with(b"/"));
    if path.starts_with(b"//") && !path.starts_with(b"///") {
        initial = 2;
    }
    let mut new: Vec<&[u8]> = Vec::new();
    for comp in path.split(|&b| b == b'/') {
        if comp.is_empty() || comp == b"." {
            continue;
        }
        if comp != b".." || (initial == 0 && new.is_empty()) || new.last() == Some(&&b".."[..]) {
            new.push(comp);
        } else if !new.is_empty() {
            new.pop();
        }
    }
    let mut out = vec![b'/'; initial];
    out.extend_from_slice(&new.join(&b'/'));
    if out.is_empty() {
        return b".".to_vec();
    }
    out
}

/// `os.path.abspath(path)` with `os.getcwd()` answering `cwd`.
pub fn abspath(flavour: Flavour, path: &[u8], cwd: &[u8]) -> Result<Vec<u8>, Unresolved> {
    if flavour == Flavour::Windows {
        return Err(Unresolved::NotPorted);
    }
    if path.starts_with(b"/") {
        return Ok(normpath(path));
    }
    // posixpath.join(cwd, path)
    let mut joined = cwd.to_vec();
    if !joined.is_empty() && !joined.ends_with(b"/") {
        joined.push(b'/');
    }
    joined.extend_from_slice(path);
    Ok(normpath(&joined))
}

/// One entry of the walk's stack: a name still to resolve, or the mark
/// that the link before it has been resolved.
enum Part {
    Name(Vec<u8>),
    Resolved(Vec<u8>),
}

/// Why a strict resolution failed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StrictUnresolved {
    /// The reference raised this: the OSError with its errno's name (and,
    /// on Windows, its code), or ValueError (a NUL).
    Raised(OsError),
    /// The Windows flavour, which is not ported.
    NotPorted,
}

/// `os.path.realpath(path)` (non-strict) with `os.getcwd()` answering
/// `cwd` (absolute and free of links, as `getcwd` gives it).
pub fn realpath<F: Fs + ?Sized>(
    fs: &F,
    flavour: Flavour,
    path: &[u8],
    cwd: &[u8],
) -> Result<Vec<u8>, Unresolved> {
    if flavour == Flavour::Windows {
        return Err(Unresolved::NotPorted);
    }
    resolve(fs, path, cwd, false).map_err(|e| Unresolved::Raised(e.class))
}

/// `os.path.realpath(path, strict=True)` (3.13.13's `posixpath`): the
/// same walk, but every OSError of an `lstat`, a `readlink` or the `stat`
/// of a loop is raised (a missing component: FileNotFoundError, ENOENT; a
/// loop: OSError, ELOOP, from that `stat`); and a component that is no
/// directory and no link with parts after it raises NotADirectoryError
/// (ENOTDIR) before anything below it is asked, a trailing separator,
/// `.` or `..` included (3.11 asks the OS instead, so `f/..` resolves
/// there: those queries are Python-gated in the fixture).
pub fn realpath_strict<F: Fs + ?Sized>(
    fs: &F,
    flavour: Flavour,
    path: &[u8],
    cwd: &[u8],
) -> Result<Vec<u8>, StrictUnresolved> {
    if flavour == Flavour::Windows {
        return Err(StrictUnresolved::NotPorted);
    }
    resolve(fs, path, cwd, true).map_err(StrictUnresolved::Raised)
}

/// The walk of both: non-strict ignores every OSError (a ValueError
/// rises); strict raises each.
fn resolve<F: Fs + ?Sized>(
    fs: &F,
    path: &[u8],
    cwd: &[u8],
    strict: bool,
) -> Result<Vec<u8>, OsError> {
    let mut rest: Vec<Part> = path
        .split(|&b| b == b'/')
        .rev()
        .map(|p| Part::Name(p.to_vec()))
        .collect();
    let mut part_count = rest.len();
    let mut resolved: Vec<u8> = if path.starts_with(b"/") {
        b"/".to_vec()
    } else {
        cwd.to_vec()
    };
    let mut seen: HashMap<Vec<u8>, Option<Vec<u8>>> = HashMap::new();
    while part_count > 0 {
        let name = match rest.pop() {
            Some(Part::Resolved(link)) => {
                seen.insert(link, Some(resolved.clone()));
                continue;
            }
            Some(Part::Name(name)) => name,
            None => break,
        };
        part_count -= 1;
        if name.is_empty() || name == b"." {
            continue;
        }
        if name == b".." {
            match resolved.iter().rposition(|&b| b == b'/') {
                Some(0) | None => resolved.truncate(1),
                Some(i) => resolved.truncate(i),
            }
            if resolved.is_empty() {
                resolved.push(b'/');
            }
            continue;
        }
        // newpath, built in place: `resolved` is it from here on.
        if resolved != b"/" {
            resolved.push(b'/');
        }
        resolved.extend_from_slice(&name);
        let kind = if strict {
            fs.lstat_result(&resolved)?.kind
        } else {
            match fs.lstat(&resolved) {
                Ok(kind) => kind,
                Err(PyErr::Value) => return Err(OsError::value()),
                // An ignored OSError: the path as reached stands.
                Err(_) => continue,
            }
        };
        if kind != Kind::Link {
            if strict && part_count > 0 && kind != Kind::Directory {
                return Err(OsError::new(PyErr::NotADirectory, "ENOTDIR"));
            }
            continue;
        }
        if let Some(cached) = seen.get(&resolved) {
            match cached {
                Some(target) => resolved = target.clone(),
                // A link still being resolved is a loop: strict raises
                // what `os.stat` of it raises (ELOOP); otherwise the path
                // stands.
                None if strict => {
                    fs.stat(&resolved)?;
                }
                None => {}
            }
            continue;
        }
        let target = if strict {
            // Strict raises what `os.readlink` raised, its errno with it.
            fs.readlink_result(&resolved)?
        } else {
            match fs.readlink(&resolved) {
                Ok(target) => target,
                Err(PyErr::Value) => return Err(OsError::value()),
                Err(_) => continue,
            }
        };
        let link = std::mem::take(&mut resolved);
        resolved = if target.starts_with(b"/") {
            b"/".to_vec()
        } else {
            // The folder the link is in.
            let i = link.iter().rposition(|&b| b == b'/').unwrap_or(0);
            let parent = &link[..i];
            if parent.is_empty() {
                b"/".to_vec()
            } else {
                parent.to_vec()
            }
        };
        seen.insert(link.clone(), None);
        rest.push(Part::Resolved(link));
        let parts: Vec<&[u8]> = target.split(|&b| b == b'/').collect();
        part_count += parts.len();
        rest.extend(parts.into_iter().rev().map(|p| Part::Name(p.to_vec())));
    }
    Ok(resolved)
}

/// What a Windows call raised: an OSError with its `winerror` (None where
/// it has none), or ValueError (a NUL).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NtRaise {
    Os(Option<i64>),
    Value,
}

/// What `ntpath.realpath` (non-strict) raises.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NtRaised {
    /// An OSError: of `os.getcwd()`, or a winerror the walk does not stop
    /// at.
    Os(Option<i64>),
    /// ValueError, from a call after the first.
    Value,
    /// UnboundLocalError: the first `_getfinalpathname` raised ValueError
    /// (so `initial_winerror` was never set), the path still gained the
    /// long prefix (a working directory spelled with it), and its plain
    /// spelling raised OSError.
    Unbound,
}

/// The Windows calls `ntpath.realpath` and `abspath` make.
pub trait FinalPath {
    /// `nt._getfinalpathname(path)`.
    fn final_path_name(&self, path: &[u32]) -> Result<PyStr, NtRaise>;
    /// `nt._getfullpathname(path)`, None where it raises.
    fn full_path_name(&self, path: &[u32]) -> Option<PyStr>;
    /// `nt.readlink(path)`.
    fn readlink(&self, path: &[u32]) -> Result<PyStr, NtRaise>;
    /// `nt._findfirstfile(path)`: the name as the folder spells it.
    fn find_first_file(&self, path: &[u32]) -> Result<PyStr, NtRaise>;
    /// `os.path.islink(path)` (`nt._path_islink`, which never raises).
    fn is_link(&self, path: &[u32]) -> bool;
}

const W: Flavour = Flavour::Windows;
/// `_readlink_deep`'s `allowed_winerror`.
const DEEP_STOPS: [i64; 12] = [1, 2, 3, 5, 21, 32, 50, 67, 87, 4390, 4392, 4393];
/// `_getfinalpathname_nonstrict`'s `allowed_winerror`.
const NONSTRICT_STOPS: [i64; 16] = [
    1, 2, 3, 5, 21, 32, 50, 53, 65, 67, 87, 123, 161, 1005, 1920, 1921,
];
/// The codes after which `_findfirstfile` is asked for the real name.
const FIND_FIRST: [i64; 7] = [1, 5, 32, 50, 87, 1920, 1921];

fn allowed(code: Option<i64>, list: &[i64]) -> bool {
    code.is_some_and(|c| list.contains(&c))
}

/// `_readlink_deep(path)`: follows links by `readlink` until a path seen
/// before (by `normcase`), a stop code or a ValueError; a relative target
/// of something that is no link answers the path before it.
fn readlink_deep<P: FinalPath + ?Sized>(p: &P, path: &[u32]) -> Result<PyStr, NtRaise> {
    let mut path = path.to_vec();
    let mut seen = std::collections::HashSet::new();
    while seen.insert(text::normcase(W, &path)) {
        let old = path.clone();
        match p.readlink(&path) {
            Ok(target) => {
                path = target;
                if !text::isabs(W, &path) {
                    if !p.is_link(&old) {
                        return Ok(old);
                    }
                    let joined = text::join(W, &text::dirname(W, &old), &[&path]);
                    path = text::normpath(W, &joined);
                }
            }
            Err(NtRaise::Os(code)) if allowed(code, &DEEP_STOPS) => break,
            Err(NtRaise::Os(code)) => return Err(NtRaise::Os(code)),
            Err(NtRaise::Value) => break,
        }
    }
    Ok(path)
}

fn joined(head: PyStr, tail: &[u32]) -> PyStr {
    if tail.is_empty() {
        head
    } else {
        text::join(W, &head, &[tail])
    }
}

/// `_getfinalpathname_nonstrict(path)`: the deepest part that resolves
/// (or that `readlink` leads elsewhere), the rest joined after it.
fn nonstrict<P: FinalPath + ?Sized>(p: &P, path: &[u32]) -> Result<PyStr, NtRaise> {
    let mut path = path.to_vec();
    let mut tail: PyStr = Vec::new();
    while !path.is_empty() {
        let code = match p.final_path_name(&path) {
            Ok(done) => return Ok(joined(done, &tail)),
            Err(NtRaise::Value) => return Err(NtRaise::Value),
            Err(NtRaise::Os(code)) if allowed(code, &NONSTRICT_STOPS) => code,
            Err(raised) => return Err(raised),
        };
        if let Ok(new) = readlink_deep(p, &path) {
            if new != path {
                return Ok(joined(new, &tail));
            }
        }
        let (head, name) = if allowed(code, &FIND_FIRST) {
            match p.find_first_file(&path) {
                Ok(name) => (text::split(W, &path).0, name),
                Err(NtRaise::Os(_)) => text::split(W, &path),
                Err(NtRaise::Value) => return Err(NtRaise::Value),
            }
        } else {
            text::split(W, &path)
        };
        path = head;
        if !path.is_empty() && name.is_empty() {
            path.extend_from_slice(&tail);
            return Ok(path);
        }
        tail = joined(name, &tail);
    }
    Ok(tail)
}

/// `ntpath.realpath(path)` (non-strict) on Windows, with `os.getcwd()`
/// answering `cwd` (asked for every path, an absolute one too, so its
/// OSError rises whatever the path).
pub fn nt_realpath<P: FinalPath + ?Sized>(
    p: &P,
    path: &[u32],
    cwd: Result<&[u32], Option<i64>>,
) -> Result<PyStr, NtRaised> {
    let mut path = text::normpath(W, path);
    let cwd = cwd.map_err(NtRaised::Os)?;
    if text::normcase(W, &path) == cps("nul") {
        return Ok(cps("\\\\.\\NUL"));
    }
    let prefix = cps("\\\\?\\");
    let had_prefix = path.starts_with(&prefix);
    if !had_prefix && !text::isabs(W, &path) {
        path = text::join(W, cwd, &[&path]);
    }
    // None: `initial_winerror` never set.
    let initial: Option<Option<i64>> = match p.final_path_name(&path) {
        Ok(done) => {
            path = done;
            Some(Some(0))
        }
        Err(NtRaise::Value) => {
            path = text::normpath(W, &path);
            None
        }
        Err(NtRaise::Os(code)) => {
            path = nonstrict(p, &path).map_err(|e| match e {
                NtRaise::Os(c) => NtRaised::Os(c),
                NtRaise::Value => NtRaised::Value,
            })?;
            Some(code)
        }
    };
    if !had_prefix && path.starts_with(&prefix) {
        let unc = cps("\\\\?\\UNC\\");
        let spath = if path.starts_with(&unc) {
            [cps("\\\\").as_slice(), &path[unc.len()..]].concat()
        } else {
            path[prefix.len()..].to_vec()
        };
        match p.final_path_name(&spath) {
            Ok(again) if again == path => path = spath,
            Ok(_) | Err(NtRaise::Value) => {}
            Err(NtRaise::Os(code)) => match initial {
                None => return Err(NtRaised::Unbound),
                Some(first) if first == code => path = spath,
                Some(_) => {}
            },
        }
    }
    Ok(path)
}

/// `ntpath.abspath(path)` on Windows: `_getfullpathname`, else the
/// path joined by hand (`os.getcwd()` answering `cwd`, its OSError
/// raised as `Err(winerror)`).
pub fn nt_abspath<P: FinalPath + ?Sized>(
    p: &P,
    path: &[u32],
    cwd: Result<&[u32], Option<i64>>,
) -> Result<PyStr, Option<i64>> {
    struct On<'a, P: ?Sized>(&'a P, Result<&'a [u32], Option<i64>>);
    impl<P: FinalPath + ?Sized> Process for On<'_, P> {
        fn getcwd(&self) -> Result<PyStr, PyErr> {
            self.1.map(<[u32]>::to_vec).map_err(|_| PyErr::Os)
        }
        fn windows(&self) -> bool {
            true
        }
        fn full_path_name(&self, path: &[u32]) -> Option<PyStr> {
            self.0.full_path_name(path)
        }
    }
    text::abspath(W, path, &On(p, cwd)).map_err(|_| cwd.err().flatten())
}
