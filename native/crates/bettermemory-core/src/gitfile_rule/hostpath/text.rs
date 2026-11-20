//! `os.path` over a Python `str` (code points, a lone surrogate allowed),
//! with the flavour as data: `posixpath` and `ntpath` as CPython 3.13.13
//! has them in Python. `join` takes any number of parts; `basename`,
//! `normcase`, `abspath` and `relpath` are the functions the byte half
//! does not have.
//!
//! `abspath` (and `relpath` through it) asks the process two things, both
//! arguments here ([`Process`]): the working directory, and, where
//! `ntpath` runs on Windows, `nt._getfullpathname`. Off Windows `ntpath`
//! has no such call and joins the working directory itself; on Windows
//! it asks first and falls back, by a rule of its own, where the call
//! raises. `ntpath.normcase` is the one off Windows (`str.lower`):
//! Windows' own asks `LCMapStringEx`, which is not ported.

use super::{alt, is_sep, nt_isabs, nt_join, nt_normpath, posix_normpath, splitroot, DOT, SEP};
use crate::fsdata::{Flavour, PyErr};
use crate::pyvalue::{repr_str, PyStr};
use crate::unicode;

const SLASH: u32 = b'/' as u32;

/// What `abspath` asks of the process it runs in.
pub trait Process {
    /// `os.getcwd()`, or the OSError it raises. Asked once for each path
    /// that needs it, as the reference asks.
    fn getcwd(&self) -> Result<PyStr, PyErr>;

    /// Whether `ntpath` runs on Windows: `nt._getfullpathname` imports.
    fn windows(&self) -> bool {
        false
    }

    /// `nt._getfullpathname(path)`, None where it raises OSError or
    /// ValueError. Asked only where [`Process::windows`] holds.
    fn full_path_name(&self, _path: &[u32]) -> Option<PyStr> {
        None
    }
}

/// What `relpath` raises.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Raised {
    /// `ValueError`, with its message.
    Value(PyStr),
    /// What `os.getcwd()` raised.
    Os(PyErr),
}

/// `os.path.sep`.
pub fn sep(flavour: Flavour) -> u32 {
    match flavour {
        Flavour::Posix => SLASH,
        Flavour::Windows => SEP,
    }
}

/// `os.path.isabs`.
pub fn isabs(flavour: Flavour, path: &[u32]) -> bool {
    match flavour {
        Flavour::Posix => path.first() == Some(&SLASH),
        Flavour::Windows => nt_isabs(path),
    }
}

/// `os.path.join(path, *parts)`.
pub fn join(flavour: Flavour, path: &[u32], parts: &[&[u32]]) -> PyStr {
    match flavour {
        Flavour::Posix => {
            let mut out = path.to_vec();
            for part in parts {
                if part.first() == Some(&SLASH) || out.is_empty() {
                    out = part.to_vec();
                    continue;
                }
                if out.last() != Some(&SLASH) {
                    out.push(SLASH);
                }
                out.extend_from_slice(part);
            }
            out
        }
        Flavour::Windows => nt_join(path, parts),
    }
}

/// `os.path.normpath`.
pub fn normpath(flavour: Flavour, path: &[u32]) -> PyStr {
    match flavour {
        Flavour::Posix => posix_normpath(path),
        Flavour::Windows => nt_normpath(path),
    }
}

/// `os.path.normcase`: the text itself under `posixpath`; `/` as `\` and
/// then `str.lower` under `ntpath` off Windows.
pub fn normcase(flavour: Flavour, text: &[u32]) -> PyStr {
    match flavour {
        Flavour::Posix => text.to_vec(),
        Flavour::Windows => {
            let swapped: PyStr = text.iter().map(|&u| alt(u)).collect();
            unicode::lower(&swapped)
        }
    }
}

/// `os.path.split`: the head and the last component.
fn split(flavour: Flavour, path: &[u32]) -> (PyStr, PyStr) {
    match flavour {
        Flavour::Posix => {
            let i = path.iter().rposition(|&u| u == SLASH).map_or(0, |i| i + 1);
            let mut head = &path[..i];
            if head.iter().any(|&u| u != SLASH) {
                let end = head.iter().rposition(|&u| u != SLASH).map_or(0, |i| i + 1);
                head = &head[..end];
            }
            (head.to_vec(), path[i..].to_vec())
        }
        Flavour::Windows => {
            let (drive, root, rest) = splitroot(path);
            let i = rest.iter().rposition(|&u| is_sep(u)).map_or(0, |i| i + 1);
            let end = rest[..i]
                .iter()
                .rposition(|&u| !is_sep(u))
                .map_or(0, |i| i + 1);
            let head = [drive.as_slice(), &root, &rest[..end]].concat();
            (head, rest[i..].to_vec())
        }
    }
}

/// `os.path.dirname`.
pub fn dirname(flavour: Flavour, path: &[u32]) -> PyStr {
    split(flavour, path).0
}

/// `os.path.basename`.
pub fn basename(flavour: Flavour, path: &[u32]) -> PyStr {
    split(flavour, path).1
}

/// `os.path.abspath`.
pub fn abspath(flavour: Flavour, path: &[u32], process: &dyn Process) -> Result<PyStr, Raised> {
    let cwd = || process.getcwd().map_err(Raised::Os);
    if flavour == Flavour::Windows && process.windows() {
        if let Some(full) = process.full_path_name(&nt_normpath(path)) {
            return Ok(full);
        }
        if nt_isabs(path) {
            return Ok(nt_normpath(path));
        }
        // A drive or a root, never both: the path is not absolute.
        let (drive, root, rest) = splitroot(path);
        let joined = if drive.is_empty() && root.is_empty() {
            nt_join(&cwd()?, &[&rest])
        } else {
            match process.full_path_name(&[drive.as_slice(), &root].concat()) {
                Some(full) => nt_join(&full, &[&rest]),
                None => [drive.as_slice(), &[SEP], &rest].concat(),
            }
        };
        return Ok(nt_normpath(&joined));
    }
    if isabs(flavour, path) {
        return Ok(normpath(flavour, path));
    }
    Ok(normpath(flavour, &join(flavour, &cwd()?, &[path])))
}

/// `os.path.relpath(path, start)`, `start` given (an empty one is the
/// working directory, as `abspath` reads it).
pub fn relpath(
    flavour: Flavour,
    path: &[u32],
    start: &[u32],
    process: &dyn Process,
) -> Result<PyStr, Raised> {
    if path.is_empty() {
        return Err(Raised::Value(unicode::cps("no path specified")));
    }
    let start_abs = abspath(flavour, start, process)?;
    let path_abs = abspath(flavour, path, process)?;
    let sep = sep(flavour);
    let (start_rest, path_rest) = match flavour {
        Flavour::Posix => {
            let tail = |p: &[u32]| {
                let lead = p.iter().take_while(|&&u| u == SLASH).count();
                p[lead..].to_vec()
            };
            (tail(&start_abs), tail(&path_abs))
        }
        Flavour::Windows => {
            let (start_drive, _, start_rest) = splitroot(&start_abs);
            let (path_drive, _, path_rest) = splitroot(&path_abs);
            if normcase(flavour, &start_drive) != normcase(flavour, &path_drive) {
                let mut message = unicode::cps("path is on mount ");
                message.extend(repr_str(&path_drive));
                message.extend(unicode::cps(", start on mount "));
                message.extend(repr_str(&start_drive));
                return Err(Raised::Value(message));
            }
            (start_rest, path_rest)
        }
    };
    let parts = |rest: &'_ [u32]| -> Vec<Vec<u32>> {
        if rest.is_empty() {
            return Vec::new();
        }
        rest.split(|&u| u == sep).map(<[u32]>::to_vec).collect()
    };
    let (start_list, path_list) = (parts(&start_rest), parts(&path_rest));
    let shared = start_list
        .iter()
        .zip(&path_list)
        .take_while(|(a, b)| match flavour {
            Flavour::Posix => a == b,
            Flavour::Windows => normcase(flavour, a) == normcase(flavour, b),
        })
        .count();
    let up = [DOT, DOT];
    let mut rel: Vec<&[u32]> = vec![&up; start_list.len() - shared];
    rel.extend(path_list[shared..].iter().map(Vec::as_slice));
    if rel.is_empty() {
        return Ok(vec![DOT]);
    }
    Ok(rel.join(&sep))
}
