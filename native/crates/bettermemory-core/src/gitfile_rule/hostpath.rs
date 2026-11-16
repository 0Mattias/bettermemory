//! The host's `os.path` as the rule's parse calls it (`isabs`, `join` of
//! two, `dirname`, `normpath`), with the flavour as data: `posixpath` for
//! [`Flavour::Posix`], `ntpath` (CPython 3.13's pure-Python functions) for
//! [`Flavour::Windows`].
//!
//! A path is bytes. `posixpath` reads only `/`, so it works on the bytes
//! as they are. `ntpath` indexes code points (a drive is the second code
//! point being `:`), so a Windows path is read as code points, UTF-8
//! decoded with each byte that does not decode kept as itself
//! (`surrogateescape`), and written back the same way.
//!
//! Where 3.11 and 3.12 read otherwise (`ntpath.isabs` of a path starting
//! with one separator, absolute before 3.13; `normpath` of a `\\?\` or
//! `\\.\` path, kept whole before 3.12), this is 3.13's reading, the one
//! the parity runner regenerates under; native/README.md lists them.

use crate::fsdata::Flavour;

/// `os.path.isabs`.
pub fn isabs(flavour: Flavour, path: &[u8]) -> bool {
    match flavour {
        Flavour::Posix => path.first() == Some(&b'/'),
        Flavour::Windows => {
            let p = units(path);
            let head: Vec<u32> = p.iter().take(3).map(|&u| alt(u)).collect();
            head.get(1..3) == Some(&[COLON, SEP][..]) || head.get(..2) == Some(&[SEP, SEP][..])
        }
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
        Flavour::Windows => bytes_of(&nt_join(&units(a), &units(b))),
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

fn posix_normpath(path: &[u8]) -> Vec<u8> {
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

/// `str.lower()` of a drive, a code point at a time (a byte kept as
/// itself is left alone).
fn lower(p: &[u32]) -> Vec<u32> {
    let mut out = Vec::with_capacity(p.len());
    for &u in p {
        match char::from_u32(u) {
            Some(c) => out.extend(c.to_lowercase().map(|l| l as u32)),
            None => out.push(u),
        }
    }
    out
}

/// `ntpath.join(a, b)`.
fn nt_join(a: &[u32], b: &[u32]) -> Vec<u32> {
    let (mut rd, mut rr, mut rp) = splitroot(a);
    let (pd, pr, pp) = splitroot(b);
    if !pr.is_empty() {
        if !pd.is_empty() || rd.is_empty() {
            rd = pd;
        }
        rr = pr;
        rp = pp;
    } else {
        let mut relative = true;
        if !pd.is_empty() && pd != rd {
            if lower(&pd) != lower(&rd) {
                rd = pd;
                rr = pr;
                rp = pp.clone();
                relative = false;
            } else {
                rd = pd;
            }
        }
        if relative {
            if rp.last().is_some_and(|&u| !is_sep(u)) {
                rp.push(SEP);
            }
            rp.extend_from_slice(&pp);
        }
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
