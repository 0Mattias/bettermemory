//! Unit 4 of `verify_candidates.py`: the claim keys (`_claim_key`,
//! `_ClaimKeys`) and the reads of the home directory and the disk
//! (`_is_under_home`, `_home_ignores_case`, `_is_multi_segment_routelike`,
//! `_path_exists`), on POSIX.
//!
//! The file system is an argument ([`Fs`]), and so is the home as unit 3
//! takes it: `home` is what `~` expands to (`$HOME`, else the running
//! user's password entry; None when neither is known) and `lookup`
//! answers `pwd.getpwnam` ([`UserHome`]). Paths are code points, as a
//! Python `str` is; each call that reaches the OS encodes its path as
//! `os.fsencode` does (UTF-8, `surrogateescape`), and a path that does not
//! encode, or that holds a NUL, raises `ValueError` before any call, as
//! CPython's `os` functions do. What the OS gives back is decoded the same
//! way (`os.fsdecode`).
//!
//! `os.path.realpath` is reproduced here over `str` (CPython 3.13.13's
//! `posixpath.realpath`, non-strict), not through the bytes walk of
//! `fsdata::realpath`: the reference keeps each component as the `str`
//! spelled it, and a decode of the encoded whole would read
//! `"\udcc3\udca9"` back as `"é"`.
//!
//! The Windows flavour (`os.name == "nt"`, `ntpath`'s `realpath` and
//! `normcase`) is not ported: on a target that is not Unix `claim_key`,
//! `ClaimKeys::key`, `home_ignores_case` and `path_exists` decline on a
//! non-empty path and `is_under_home` on every path
//! ([`Raised::NotPorted`]); `is_multi_segment_routelike` answers False
//! where the text alone decides, as the reference does there too, and
//! otherwise declines through `is_under_home`; `swapcase` reads no path.

use std::collections::HashMap;

use super::scan::{normalize_for_compare, CompareError, HomeError, UserHome, UserLookup};
use super::validate::{fold_altsep, is_lower, refused_spelling};
use crate::fsdata::{samestat, Fs, Kind, PyErr};
use crate::gitfile_rule::follow::fsencode;
use crate::gitsystem::record::surrogateescape;
use crate::unicode::{self, is_case_ignorable, is_cased};

const SLASH: u32 = '/' as u32;
const TILDE: u32 = '~' as u32;
const DOT: u32 = '.' as u32;
const SIGMA: u32 = 0x3A3;

/// `sys.getrecursionlimit()`'s default, the bound `_ClaimKeys`' recursive
/// `_resolve` meets on a deep path.
pub const RECURSION_LIMIT: usize = 1000;

/// What escapes a function of this unit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Raised {
    /// `ValueError` out of `os.path.expanduser`: `pwd.getpwnam` refused a
    /// `~user` name ([`UserHome::Refused`]). No handler of the reference
    /// is around that call.
    Value,
    /// `RecursionError` out of `_ClaimKeys._resolve` (one Python frame per
    /// component it has not resolved before).
    Recursion,
    /// `RuntimeError` out of `Path.expanduser` (`_path_exists`).
    Home(HomeError),
    /// The Windows flavour, or a `samestat` this file system does not
    /// port: a decline.
    NotPorted,
}

/// `os.fsencode` of a path an `os` call takes: None where the call raises
/// `ValueError` before reaching the OS (a code point that does not encode,
/// or a NUL).
fn encode(path: &[u32]) -> Option<Vec<u8>> {
    fsencode(path).filter(|b| !b.contains(&0))
}

fn cat(parts: &[&[u32]]) -> Vec<u32> {
    parts.concat()
}

/// `posixpath.expanduser` of a `str`.
fn expanduser(
    path: &[u32],
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Result<Vec<u32>, Raised> {
    if path.first() != Some(&TILDE) {
        return Ok(path.to_vec());
    }
    let i = path[1..]
        .iter()
        .position(|&c| c == SLASH)
        .map_or(path.len(), |j| j + 1);
    let userhome = if i == 1 {
        match home {
            Some(h) => h.to_vec(),
            None => return Ok(path.to_vec()),
        }
    } else {
        match lookup(&path[1..i]) {
            UserHome::Found(h) => h,
            UserHome::Unknown => return Ok(path.to_vec()),
            UserHome::Refused => return Err(Raised::Value),
        }
    };
    let end = userhome
        .iter()
        .rposition(|&c| c != SLASH)
        .map_or(0, |k| k + 1);
    let out = cat(&[&userhome[..end], &path[i..]]);
    Ok(if out.is_empty() { vec![SLASH] } else { out })
}

/// `os.path.islink`: `lstat` says link; any error is False.
fn islink<F: Fs + ?Sized>(fs: &F, path: &[u32]) -> bool {
    encode(path).is_some_and(|b| fs.lstat(&b) == Ok(Kind::Link))
}

/// `os.path.isdir`: `stat` (links followed) says directory; any error is
/// False.
fn isdir<F: Fs + ?Sized>(fs: &F, path: &[u32]) -> bool {
    encode(path).is_some_and(|b| fs.stat(&b).is_ok_and(|s| s.kind == Kind::Directory))
}

/// One entry of the walk's stack: a name, or the link whose target was
/// just resolved (the reference pushes the link and a `None`).
enum Part {
    Name(Vec<u32>),
    Done(Vec<u32>),
}

/// A path as code points and as the bytes `os.fsencode` makes of them.
type Spelled = (Vec<u32>, Vec<u8>);

/// `posixpath.realpath(filename)` (3.13.13, non-strict) of an absolute
/// `str`: each OSError leaves the path as reached, a link met again while
/// it is being resolved (a loop) stands as it is, and `Err(())` is the
/// `ValueError` of an `lstat` or `readlink` whose path does not encode.
///
/// The path is kept twice, as code points and as the bytes `os.fsencode`
/// makes of them, and each name is appended to both in place: a name is
/// encoded once, not with every prefix the walk asks about. The encoding
/// is one code point at a time, so the bytes of the whole are the bytes
/// of its parts, and `/` is the only code point whose bytes hold `0x2F`.
fn realpath<F: Fs + ?Sized>(fs: &F, filename: &[u32]) -> Result<Vec<u32>, ()> {
    let mut rest: Vec<Part> = filename
        .split(|&c| c == SLASH)
        .rev()
        .map(|p| Part::Name(p.to_vec()))
        .collect();
    let mut part_count = rest.len();
    let mut path = vec![SLASH];
    let mut bytes = vec![b'/'];
    let mut seen: HashMap<Vec<u32>, Option<Spelled>> = HashMap::new();
    while part_count > 0 {
        let name = match rest.pop() {
            Some(Part::Done(link)) => {
                seen.insert(link, Some((path.clone(), bytes.clone())));
                continue;
            }
            Some(Part::Name(name)) => name,
            None => break,
        };
        part_count -= 1;
        if name.is_empty() || name == [DOT] {
            continue;
        }
        if name == [DOT, DOT] {
            path.truncate(path.iter().rposition(|&c| c == SLASH).unwrap_or(0));
            bytes.truncate(bytes.iter().rposition(|&b| b == b'/').unwrap_or(0));
            if path.is_empty() {
                path.push(SLASH);
                bytes.push(b'/');
            }
            continue;
        }
        let encoded = encode(&name).ok_or(())?;
        let (held, held_bytes) = (path.len(), bytes.len());
        if path != [SLASH] {
            path.push(SLASH);
            bytes.push(b'/');
        }
        path.extend_from_slice(&name);
        bytes.extend_from_slice(&encoded);
        // `path` and `bytes` are now the reference's `newpath`.
        match fs.lstat(&bytes) {
            Ok(Kind::Link) => {}
            Err(PyErr::Value) => return Err(()),
            Ok(_) | Err(_) => continue,
        }
        if let Some(cached) = seen.get(&path) {
            if let Some((p, b)) = cached {
                (path, bytes) = (p.clone(), b.clone());
            }
            continue;
        }
        let target = match fs.readlink(&bytes) {
            Ok(target) => surrogateescape(&target),
            Err(PyErr::Value) => return Err(()),
            Err(_) => continue,
        };
        let newpath = path.clone();
        if target.first() == Some(&SLASH) {
            (path, bytes) = (vec![SLASH], vec![b'/']);
        } else {
            path.truncate(held);
            bytes.truncate(held_bytes);
        }
        seen.insert(newpath.clone(), None);
        rest.push(Part::Done(newpath));
        let parts: Vec<&[u32]> = target.split(|&c| c == SLASH).collect();
        part_count += parts.len();
        rest.extend(parts.into_iter().rev().map(|p| Part::Name(p.to_vec())));
    }
    Ok(path)
}

/// `posixpath.split`: the head with its trailing slashes cut (unless it is
/// all slashes) and the name after the last slash.
fn split(p: &[u32]) -> (&[u32], &[u32]) {
    let i = p.iter().rposition(|&c| c == SLASH).map_or(0, |k| k + 1);
    let (head, tail) = (&p[..i], &p[i..]);
    if head.iter().all(|&c| c == SLASH) {
        return (head, tail);
    }
    let end = head.iter().rposition(|&c| c != SLASH).map_or(0, |k| k + 1);
    (&head[..end], tail)
}

/// `posixpath.dirname`.
fn dirname(p: &[u32]) -> Vec<u32> {
    split(p).0.to_vec()
}

/// `_claim_key(path)` on POSIX: `""` for `""`; a refused spelling as it is
/// (`normcase` is the identity); else `expanduser`, and an absolute result
/// resolved by `realpath`, its `ValueError` keeping the expanded spelling.
/// `expanduser`'s `ValueError` escapes ([`Raised::Value`]).
pub fn claim_key<F: Fs + ?Sized>(
    fs: &F,
    path: &[u32],
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Result<Vec<u32>, Raised> {
    if path.is_empty() {
        return Ok(Vec::new());
    }
    if cfg!(not(unix)) {
        return Err(Raised::NotPorted);
    }
    if refused_spelling(path) {
        return Ok(path.to_vec());
    }
    let expanded = expanduser(path, home, lookup)?;
    if expanded.first() != Some(&SLASH) {
        return Ok(expanded);
    }
    Ok(realpath(fs, &expanded).unwrap_or(expanded))
}

/// `_ClaimKeys`: `_claim_key` for one read, each path keyed once and each
/// directory on the way resolved once, one component at a time on top of
/// its parent's memoised resolution (`_resolve`), never shared across
/// reads.
///
/// `_resolve` recurses once per component not resolved before, so a deep
/// path raises `RecursionError`. `room` is how many Python frames the
/// reference's caller had left above its own frame
/// (`sys.getrecursionlimit()` less the caller's depth): a key needs four
/// frames (`__call__`, `_key`, `os.path.isabs` and its `_get_sep`), six
/// for a `~` or `~/...` path (`os.path.expanduser` reads `os.environ`
/// through `Mapping.__contains__`, `_Environ.__getitem__` and its
/// `encodekey`), and at least four plus one per `_resolve` that misses the
/// memo, the deepest being the last miss's `os.path.split` and its
/// `_get_sep`. A key that needs more than
/// `room` raises before any call reaches the disk and leaves both memos as
/// they were. The walk here is a loop: no depth reaches Rust's stack.
pub struct ClaimKeys {
    keys: HashMap<Vec<u32>, Vec<u32>>,
    resolved: HashMap<Vec<u32>, Vec<u32>>,
    room: usize,
}

impl ClaimKeys {
    /// A fresh memo for one read, its caller with `room` frames left.
    pub fn new(room: usize) -> ClaimKeys {
        ClaimKeys {
            keys: HashMap::new(),
            resolved: HashMap::new(),
            room,
        }
    }

    /// `__call__`: the memoised key, else `_key`, kept when it answers.
    pub fn key<F: Fs + ?Sized>(
        &mut self,
        fs: &F,
        path: &[u32],
        home: Option<&[u32]>,
        lookup: &mut UserLookup<'_>,
    ) -> Result<Vec<u32>, Raised> {
        if self.room == 0 {
            return Err(Raised::Recursion);
        }
        if let Some(key) = self.keys.get(path) {
            return Ok(key.clone());
        }
        let key = self.key_once(fs, path, home, lookup)?;
        self.keys.insert(path.to_vec(), key.clone());
        Ok(key)
    }

    fn key_once<F: Fs + ?Sized>(
        &mut self,
        fs: &F,
        path: &[u32],
        home: Option<&[u32]>,
        lookup: &mut UserLookup<'_>,
    ) -> Result<Vec<u32>, Raised> {
        // The frames up to the deepest call before `_resolve`: `_key`'s
        // own, `_refused_spelling`, `expanduser` or `isabs` and its
        // `_get_sep`. A `~` or `~/...` path has `expanduser` read
        // `'HOME' not in os.environ`, three Python frames deeper
        // (`Mapping.__contains__`, `_Environ.__getitem__` and its
        // `encodekey`), whether `HOME` is set or not; a `~user` path asks
        // `pwd.getpwnam`, which is C.
        let base = if path.is_empty() {
            2
        } else if refused_spelling(path) {
            3
        } else if path.first() == Some(&TILDE) && path.get(1).is_none_or(|&c| c == SLASH) {
            6
        } else {
            4
        };
        if base > self.room {
            return Err(Raised::Recursion);
        }
        if path.is_empty() {
            return Ok(Vec::new());
        }
        if cfg!(not(unix)) {
            return Err(Raised::NotPorted);
        }
        if refused_spelling(path) {
            return Ok(path.to_vec());
        }
        let expanded = expanduser(path, home, lookup)?;
        if expanded.first() != Some(&SLASH) {
            return Ok(expanded);
        }
        self.resolve(fs, &expanded)
    }

    /// `_resolve(path)` for an absolute `path`, as a loop: down through
    /// `split` to a memoised path or the root, then back up.
    fn resolve<F: Fs + ?Sized>(&mut self, fs: &F, path: &[u32]) -> Result<Vec<u32>, Raised> {
        let mut chain: Vec<(&[u32], &[u32])> = Vec::new();
        let mut at = path;
        let (bottom, root) = loop {
            if let Some(held) = self.resolved.get(at) {
                break (held.clone(), false);
            }
            let (parent, name) = split(at);
            if name.is_empty() && parent == at {
                break (Vec::new(), true);
            }
            chain.push((at, name));
            at = parent;
        };
        let misses = chain.len() + usize::from(root);
        if 4 + misses > self.room {
            return Err(Raised::Recursion);
        }
        let mut resolved = if root {
            // `realpath` of a path of slashes alone reads nothing.
            let r = realpath(fs, at).unwrap_or_else(|()| vec![SLASH]);
            self.resolved.insert(at.to_vec(), r.clone());
            r
        } else {
            bottom
        };
        for (p, name) in chain.into_iter().rev() {
            resolved = if name.is_empty() || name == [DOT] {
                resolved
            } else if name == [DOT, DOT] {
                dirname(&resolved)
            } else {
                let joined = if resolved.last() == Some(&SLASH) {
                    cat(&[&resolved, name])
                } else {
                    cat(&[&resolved, &[SLASH], name])
                };
                if islink(fs, &joined) {
                    realpath(fs, &joined).unwrap_or(joined)
                } else {
                    joined
                }
            };
            self.resolved.insert(p.to_vec(), resolved.clone());
        }
        Ok(resolved)
    }
}

/// `str.isupper` of one code point: the `Uppercase` property (`Lu` and
/// `Other_Uppercase`, Unicode 15.1.0).
fn is_upper(cp: u32) -> bool {
    const OTHER_UPPERCASE: &[(u32, u32)] = &[
        (0x2160, 0x216F),
        (0x24B6, 0x24CF),
        (0x1F130, 0x1F149),
        (0x1F150, 0x1F169),
        (0x1F170, 0x1F189),
    ];
    if cp < 0x80 {
        return (cp as u8).is_ascii_uppercase();
    }
    unicode::category(cp) == "Lu" || OTHER_UPPERCASE.iter().any(|&(a, b)| (a..=b).contains(&cp))
}

/// CPython's `handle_capital_sigma`: U+03A3 at `i` is final when a cased
/// code point comes before it and none after it, case-ignorable code
/// points skipped both ways.
fn final_sigma(s: &[u32], i: usize) -> bool {
    let before = s[..i].iter().rev().find(|&&c| !is_case_ignorable(c));
    if !before.is_some_and(|&c| is_cased(c)) {
        return false;
    }
    match s[i + 1..].iter().find(|&&c| !is_case_ignorable(c)) {
        Some(&c) => !is_cased(c),
        None => true,
    }
}

/// `str.swapcase` (CPython's `do_swapcase`): an uppercase code point
/// lowered with its full mapping (U+03A3 as final sigma in context), a
/// lowercase one uppered with its full mapping, every other unchanged.
pub fn swapcase(s: &[u32]) -> Vec<u32> {
    let mut out = Vec::with_capacity(s.len());
    for (i, &c) in s.iter().enumerate() {
        if is_upper(c) {
            if c == SIGMA {
                out.push(if final_sigma(s, i) { 0x3C2 } else { 0x3C3 });
            } else {
                out.extend(unicode::lower(&[c]));
            }
        } else if is_lower(c) {
            out.extend(unicode::upper(&[c]));
        } else {
            out.push(c);
        }
    }
    out
}

/// `_home_ignores_case(home)`: False when `str.swapcase` changes nothing;
/// else `os.path.samefile(home, flipped)`, False on any error. The first
/// `stat` is made before the flipped spelling is encoded, as `samefile`
/// makes it. A file system whose `samestat` is not ported declines, and
/// so does a non-empty home on a target that is not Unix (the reference
/// encodes there with `surrogatepass`).
pub fn home_ignores_case<F: Fs + ?Sized>(fs: &F, home: &[u32]) -> Result<bool, Raised> {
    if cfg!(not(unix)) && !home.is_empty() {
        return Err(Raised::NotPorted);
    }
    let flipped = swapcase(home);
    if flipped == home {
        return Ok(false);
    }
    let Some(first) = encode(home).and_then(|b| fs.stat(&b).ok()) else {
        return Ok(false);
    };
    let Some(second) = encode(&flipped).and_then(|b| fs.stat(&b).ok()) else {
        return Ok(false);
    };
    samestat(&first, &second).ok_or(Raised::NotPorted)
}

/// `_is_under_home(s)` on POSIX (`os.sep` `/`, `os.altsep` None): the home
/// from `expanduser("~")`, disabled when unknown or the root; the exact
/// or prefix test first; then `str.lower` of both and, only where they
/// agree modulo case, the volume's probe. `expanduser(s)`'s `ValueError`
/// escapes.
pub fn is_under_home<F: Fs + ?Sized>(
    fs: &F,
    s: &[u32],
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Result<bool, Raised> {
    if cfg!(not(unix)) {
        return Err(Raised::NotPorted);
    }
    let sep = [SLASH];
    let h = expanduser(&[TILDE], home, lookup)?;
    if h.is_empty() || h == [TILDE] {
        return Ok(false);
    }
    let h = fold_altsep(&h, &sep, None);
    if h == sep {
        return Ok(false);
    }
    let expanded = fold_altsep(&expanduser(s, home, lookup)?, &sep, None);
    let prefix = if h.last() == Some(&SLASH) {
        h.clone()
    } else {
        cat(&[&h, &sep])
    };
    if expanded == h || expanded.starts_with(&prefix) {
        return Ok(true);
    }
    let lowered = unicode::lower(&expanded);
    if lowered != unicode::lower(&h) && !lowered.starts_with(&unicode::lower(&prefix)) {
        return Ok(false);
    }
    home_ignores_case(fs, &h)
}

/// `_is_multi_segment_routelike(s)`: a leading `/` and at least two
/// slashes; no `.` in the text after the last slash; not under the home;
/// and the text before the last slash, `~`-expanded, not a directory.
pub fn is_multi_segment_routelike<F: Fs + ?Sized>(
    fs: &F,
    s: &[u32],
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Result<bool, Raised> {
    if s.first() != Some(&SLASH) || s.iter().filter(|&&c| c == SLASH).count() < 2 {
        return Ok(false);
    }
    let i = s.iter().rposition(|&c| c == SLASH).unwrap_or(0);
    let (parent, last) = (&s[..i], &s[i + 1..]);
    if last.contains(&DOT) {
        return Ok(false);
    }
    if is_under_home(fs, s, home, lookup)? {
        return Ok(false);
    }
    Ok(!isdir(fs, &expanduser(parent, home, lookup)?))
}

/// `_path_exists(candidate)`: `Path(candidate).expanduser().exists()`,
/// links followed. False on any OSError and on a `ValueError` (a path
/// that does not encode, or a `~user` name `getpwnam` refuses);
/// `Path.expanduser`'s `RuntimeError` escapes ([`Raised::Home`]). An empty
/// candidate is `Path("")`, which is `.`.
pub fn path_exists<F: Fs + ?Sized>(
    fs: &F,
    candidate: &[u32],
    home: Option<&[u32]>,
    lookup: &mut UserLookup<'_>,
) -> Result<bool, Raised> {
    let spelled = if candidate.is_empty() {
        vec![DOT]
    } else {
        let mut refused = false;
        let mut spy = |name: &[u32]| {
            let answer = lookup(name);
            refused |= answer == UserHome::Refused;
            answer
        };
        match normalize_for_compare(candidate, home, &mut spy) {
            Ok(_) if refused => return Ok(false),
            Ok(s) => s,
            Err(CompareError::Home(e)) => return Err(Raised::Home(e)),
            Err(CompareError::NotPorted) => return Err(Raised::NotPorted),
        }
    };
    Ok(encode(&spelled).is_some_and(|b| fs.stat(&b).is_ok()))
}
