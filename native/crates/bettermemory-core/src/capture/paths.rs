//! `_paths` (58 lines at ececad8c): whether one recorded path lies at or
//! below another, with separators read off the spelling and never off the
//! host, so no path flavour is needed. A POSIX spelling (starting ``/``)
//! uses ``/`` alone; any other uses ``/`` and ``\``.

use crate::pyvalue::PyStr;
use crate::unicode;

const SLASH: u32 = b'/' as u32;
const BACKSLASH: u32 = b'\\' as u32;

fn separators(path: &[u32]) -> &'static [u32] {
    if path.first() == Some(&SLASH) {
        &[SLASH]
    } else {
        &[SLASH, BACKSLASH]
    }
}

fn rstrip<'a>(t: &'a [u32], set: &[u32]) -> &'a [u32] {
    let end = t
        .iter()
        .rposition(|c| !set.contains(c))
        .map_or(0, |i| i + 1);
    &t[..end]
}

/// `_paths.norm`: trailing separators trimmed, a root (``/``, ``C:\``)
/// kept.
pub fn norm(path: &[u32]) -> PyStr {
    let stripped = rstrip(path, separators(path));
    if stripped.len() == 2
        && unicode::is_alpha(stripped[0])
        && stripped[1] == u32::from(b':')
        && path.len() > 2
        && (path[2] == SLASH || path[2] == BACKSLASH)
    {
        return path[..3].to_vec();
    }
    if stripped.is_empty() {
        path.to_vec()
    } else {
        stripped.to_vec()
    }
}

/// `_paths.inside`: `path` is `root` or below it, compared as spelled.
pub fn inside(path: &[u32], root: &[u32]) -> bool {
    let (path, root) = (norm(path), norm(root));
    if path == root {
        return true;
    }
    separators(&root).iter().any(|&sep| {
        let mut prefix = root.clone();
        if prefix.last() != Some(&sep) {
            prefix.push(sep);
        }
        path.starts_with(&prefix)
    })
}

/// `_paths.basename`: the last recorded component, a POSIX backslash
/// kept as data.
pub fn basename(path: &[u32]) -> PyStr {
    let mut spelled = norm(path);
    if spelled.first() != Some(&SLASH) {
        for c in spelled.iter_mut() {
            if *c == BACKSLASH {
                *c = SLASH;
            }
        }
    }
    let trimmed = rstrip(&spelled, &[SLASH]);
    let last = match trimmed.iter().rposition(|&c| c == SLASH) {
        Some(i) => &trimmed[i + 1..],
        None => trimmed,
    };
    if last.is_empty() {
        path.to_vec()
    } else {
        last.to_vec()
    }
}
