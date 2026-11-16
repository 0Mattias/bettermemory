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
//! The Windows flavour (`ntpath.realpath` over `_getfinalpathname` and
//! its fallbacks, `ntpath.abspath` over `_getfullpathname`) is not ported:
//! it answers [`Unresolved::NotPorted`], which a caller reads as a decline.

use std::collections::HashMap;

use super::{Flavour, Fs, Kind, PyErr};

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
        let kind = match fs.lstat(&resolved) {
            Ok(kind) => kind,
            Err(PyErr::Value) => return Err(Unresolved::Raised(PyErr::Value)),
            // An ignored OSError: the path as reached stands.
            Err(_) => continue,
        };
        if kind != Kind::Link {
            continue;
        }
        if let Some(cached) = seen.get(&resolved) {
            // A link still being resolved (None) is a loop: the path stands.
            if let Some(target) = cached {
                resolved = target.clone();
            }
            continue;
        }
        let target = match fs.readlink(&resolved) {
            Ok(target) => target,
            Err(PyErr::Value) => return Err(Unresolved::Raised(PyErr::Value)),
            Err(_) => continue,
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
