//! The host's `os.path` as the rule's parse calls it (`isabs`, `join` of
//! two, `dirname`, `normpath`), with the flavour as data: `posixpath` for
//! [`Flavour::Posix`], `ntpath` (CPython 3.13's pure-Python functions) for
//! [`Flavour::Windows`].
//!
//! A path is bytes. `posixpath` reads only `/`, so it works on the bytes
//! as they are. `ntpath` indexes code points (a drive is the second code
//! point being `:`), so a Windows path is read as code points, UTF-8
//! decoded with each byte that does not decode kept as itself
//! (`surrogateescape`), and written back the same way. A path that is a
//! `str` already ([`isabs_str`]) is read as its code points, a lone
//! surrogate included.
//!
//! Where 3.11 and 3.12 read otherwise (`ntpath.isabs` of a path starting
//! with one separator, absolute before 3.13; `normpath` of a `\\?\` or
//! `\\.\` path, kept whole before 3.12), this is 3.13's reading, the one
//! the parity runner regenerates under; native/README.md lists them.
//!
//! [`text`] holds the same module over a Python `str` (code points), with
//! the functions the parse does not call: `join` of any number of parts,
//! `basename`, `normcase`, `abspath` and `relpath`.

pub mod text;

use crate::fsdata::Flavour;
use crate::unicode;

/// `os.path.isabs`.
pub fn isabs(flavour: Flavour, path: &[u8]) -> bool {
    match flavour {
        Flavour::Posix => path.first() == Some(&b'/'),
        Flavour::Windows => nt_isabs(&units(path)),
    }
}

/// `os.path.isabs` of a `str`, as code points. `posixpath`: the first is
/// `/`. `ntpath` (3.13 and later): the first three, `/` read as `\`,
/// start with two separators (UNC, device) or hold a colon and a
/// separator after the first (a drive and root); a path rooted without a
/// drive (`/g`, `\g`) is not absolute, where 3.11 and 3.12 read it as
/// absolute.
pub fn isabs_str(flavour: Flavour, path: &[u32]) -> bool {
    match flavour {
        Flavour::Posix => path.first() == Some(&ALTSEP),
        Flavour::Windows => nt_isabs(path),
    }
}

/// `os.path.join(a, b)`.
pub fn join(flavour: Flavour, a: &[u8], b: &[u8]) -> Vec<u8> {
    match flavour {
        Flavour::Posix => {
            if b.first() == Some(&b'/') {
                return b.to_vec();
            }
            let mut out = a.to_vec();
            if !out.is_empty() && out.last() != Some(&b'/') {
                out.push(b'/');
            }
            out.extend_from_slice(b);
            out
        }
        Flavour::Windows => bytes_of(&nt_join(&units(a), &[&units(b)])),
    }
}

/// `os.path.dirname`.
pub fn dirname(flavour: Flavour, path: &[u8]) -> Vec<u8> {
    match flavour {
        Flavour::Posix => {
            let i = path.iter().rposition(|&b| b == b'/').map_or(0, |i| i + 1);
            let head = &path[..i];
            if !head.is_empty() && head.iter().any(|&b| b != b'/') {
                let end = head.iter().rposition(|&b| b != b'/').map_or(0, |i| i + 1);
                return head[..end].to_vec();
            }
            head.to_vec()
        }
        Flavour::Windows => {
            let (d, r, p) = splitroot(&units(path));
            let mut i = p.len();
            while i > 0 && !is_sep(p[i - 1]) {
                i -= 1;
            }
            let mut head = &p[..i];
            while let Some((&last, rest)) = head.split_last() {
                if !is_sep(last) {
                    break;
                }
                head = rest;
            }
            bytes_of(&[d, r, head.to_vec()].concat())
        }
    }
}

/// `os.path.normpath`.
pub fn normpath(flavour: Flavour, path: &[u8]) -> Vec<u8> {
    match flavour {
        Flavour::Posix => posix_normpath(path),
        Flavour::Windows => bytes_of(&nt_normpath(&units(path))),
    }
}

/// `posixpath.normpath`, over bytes or code points.
fn posix_normpath<T: Copy + PartialEq + From<u8>>(path: &[T]) -> Vec<T> {
    let (slash, dot) = (T::from(b'/'), T::from(b'.'));
    if path.is_empty() {
        return vec![dot];
    }
    // One leading slash, or two where there are exactly two.
    let leading = path.iter().take_while(|&&c| c == slash).count();
    let initial = if leading == 2 { 2 } else { leading.min(1) };
    let mut new: Vec<&[T]> = Vec::new();
    for comp in path.split(|&c| c == slash) {
        if comp.is_empty() || comp == [dot] {
            continue;
        }
        if comp != [dot, dot]
            || (initial == 0 && new.is_empty())
            || new.last().is_some_and(|last| *last == [dot, dot])
        {
            new.push(comp);
        } else if !new.is_empty() {
            new.pop();
        }
    }
    let mut out = vec![slash; initial];
    out.extend_from_slice(&new.join(&slash));
    if out.is_empty() {
        return vec![dot];
    }
    out
}

// ---------------------------------------------------------------------------
// ntpath, over code points
// ---------------------------------------------------------------------------

const SEP: u32 = b'\\' as u32;
const ALTSEP: u32 = b'/' as u32;
const COLON: u32 = b':' as u32;
const DOT: u32 = b'.' as u32;

fn alt(u: u32) -> u32 {
    if u == ALTSEP {
        SEP
    } else {
        u
    }
}

fn is_sep(u: u32) -> bool {
    u == SEP || u == ALTSEP
}

/// Code points, a byte that does not decode as U+DC80 plus the byte.
fn units(path: &[u8]) -> Vec<u32> {
    let mut out = Vec::with_capacity(path.len());
    for chunk in path.utf8_chunks() {
        out.extend(chunk.valid().chars().map(|c| c as u32));
        out.extend(chunk.invalid().iter().map(|&b| 0xDC00 + u32::from(b)));
    }
    out
}

fn bytes_of(units: &[u32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(units.len());
    for &u in units {
        match char::from_u32(u) {
            Some(c) => {
                let mut buf = [0; 4];
                out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            }
            // Only an escaped byte (U+DC80 to U+DCFF) is not a char here.
            None => out.push((u - 0xDC00) as u8),
        }
    }
    out
}

fn find(p: &[u32], what: u32, from: usize) -> Option<usize> {
    p.get(from..)?
        .iter()
        .position(|&u| u == what)
        .map(|i| i + from)
}

/// `ntpath.splitroot`: drive, root and tail.
fn splitroot(p: &[u32]) -> (Vec<u32>, Vec<u32>, Vec<u32>) {
    let normp: Vec<u32> = p.iter().map(|&u| alt(u)).collect();
    let empty = Vec::new;
    if normp.first() == Some(&SEP) {
        if normp.get(1) == Some(&SEP) {
            let unc: Vec<u32> = b"\\\\?\\UNC\\".iter().map(|&b| u32::from(b)).collect();
            let upper: Vec<u32> = normp
                .iter()
                .take(8)
                .map(|&u| match char::from_u32(u) {
                    // Only an ASCII letter can upper to one of "UNC".
                    Some(c) if c.is_ascii() => c.to_ascii_uppercase() as u32,
                    _ => u,
                })
                .collect();
            let start = if upper == unc { 8 } else { 2 };
            let Some(index) = find(&normp, SEP, start) else {
                return (p.to_vec(), empty(), empty());
            };
            let Some(index2) = find(&normp, SEP, index + 1) else {
                return (p.to_vec(), empty(), empty());
            };
            return (
                p[..index2].to_vec(),
                p[index2..index2 + 1].to_vec(),
                p[index2 + 1..].to_vec(),
            );
        }
        return (empty(), p[..1].to_vec(), p[1..].to_vec());
    }
    if normp.get(1) == Some(&COLON) {
        if normp.get(2) == Some(&SEP) {
            return (p[..2].to_vec(), p[2..3].to_vec(), p[3..].to_vec());
        }
        return (p[..2].to_vec(), empty(), p[2..].to_vec());
    }
    (empty(), empty(), p.to_vec())
}

/// `ntpath.isabs`: a drive and a root, or two leading separators.
fn nt_isabs(p: &[u32]) -> bool {
    let head: Vec<u32> = p.iter().take(3).map(|&u| alt(u)).collect();
    head.get(1..3) == Some(&[COLON, SEP][..]) || head.get(..2) == Some(&[SEP, SEP][..])
}

/// `ntpath.join(path, *paths)`. Two drives are one where their
/// `str.lower()` is equal: Python's, of the Unicode data the crate
/// carries, with its final sigma (a byte kept as itself has no case).
fn nt_join(path: &[u32], paths: &[&[u32]]) -> Vec<u32> {
    let (mut rd, mut rr, mut rp) = splitroot(path);
    for p in paths {
        let (pd, pr, pp) = splitroot(p);
        if !pr.is_empty() {
            if !pd.is_empty() || rd.is_empty() {
                rd = pd;
            }
            rr = pr;
            rp = pp;
            continue;
        }
        if !pd.is_empty() && pd != rd {
            if unicode::lower(&pd) != unicode::lower(&rd) {
                rd = pd;
                rr = pr;
                rp = pp;
                continue;
            }
            rd = pd;
        }
        if rp.last().is_some_and(|&u| !is_sep(u)) {
            rp.push(SEP);
        }
        rp.extend_from_slice(&pp);
    }
    if !rp.is_empty() && rr.is_empty() && rd.last().is_some_and(|&u| u != COLON && !is_sep(u)) {
        return [rd, vec![SEP], rp].concat();
    }
    [rd, rr, rp].concat()
}

/// `ntpath.normpath` (CPython 3.13's pure-Python one).
fn nt_normpath(path: &[u32]) -> Vec<u32> {
    let path: Vec<u32> = path.iter().map(|&u| alt(u)).collect();
    let (drive, root, rest) = splitroot(&path);
    // The reference deletes from a list in place; the components before
    // its index are always those kept so far, so a stack reads alike in
    // linear time.
    let mut comps: Vec<&[u32]> = Vec::new();
    for comp in rest.split(|&u| u == SEP) {
        if comp.is_empty() || comp == [DOT] {
            continue;
        }
        if comp == [DOT, DOT] {
            match comps.last() {
                Some(last) if *last != [DOT, DOT] => {
                    comps.pop();
                    continue;
                }
                None if !root.is_empty() => continue,
                _ => {}
            }
        }
        comps.push(comp);
    }
    let prefix_empty = drive.is_empty() && root.is_empty();
    let dot = [DOT];
    if prefix_empty && comps.is_empty() {
        comps.push(&dot);
    }
    let mut out = [drive, root].concat();
    for (n, comp) in comps.iter().enumerate() {
        if n > 0 {
            out.push(SEP);
        }
        out.extend_from_slice(comp);
    }
    out
}
