//! What git's answers say: `gitsystem.py`'s `_own_files`, `_variables`,
//! `_REMOTE_ANSWERS`, the reading of the answers inside `_reads_remotes`,
//! the parse inside `_older_system` and `_absolute`, and
//! `gitversion.parse`.
//!
//! Pure functions over what git printed. No process, file system, clock
//! or environment. What `os.path`, `os.fsdecode` and `os.name` decide is
//! the [`Flavour`] argument, so the same answers come out on every host:
//! POSIX decodes a path as `os.fsdecode` does there (UTF-8, an invalid
//! byte as a lone surrogate), Windows as it does there (UTF-8 with
//! `surrogatepass`, an invalid byte raising UnicodeDecodeError).
//!
//! The ask (`_ask`, `_older_system`'s question, `_reads_remotes`' three
//! questions and the probe's checks) is unit 4's: its caller hands in
//! what each question answered.

use std::collections::{HashMap, HashSet};

use crate::fsdata::Flavour;
use crate::pyvalue::{self, PyStr, PyValue};
use crate::unicode;

/// `_OWN_MAX`.
pub const OWN_MAX: usize = 8;
/// `_MAX_PATH`.
pub const MAX_PATH: usize = 4096;
/// `_PREFIX_NAME`.
pub const PREFIX_NAME: &str = "bettermemory.prefix";
/// `_PREFIX_TAIL`.
pub const PREFIX_TAIL: &str = "/z";
/// `_HASCONFIG_NAME`.
pub const HASCONFIG_NAME: &str = "bettermemory.hasconfig";

/// What the reference raises out of a parse.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Raised {
    /// `int()` of a part past 4,300 digits.
    ValueError,
    /// Windows' `os.fsdecode` of bytes that are not UTF-8.
    UnicodeDecodeError,
}

impl Raised {
    pub fn name(self) -> &'static str {
        match self {
            Raised::ValueError => "ValueError",
            Raised::UnicodeDecodeError => "UnicodeDecodeError",
        }
    }
}

/// What a question answered: its exit status and its stdout as bytes;
/// None where git did not answer (could not run, or out of time).
pub type Done<'a> = Option<(i32, &'a [u8])>;

/// `_Probed`: `legacy`, `resets`, `prefix`, `compat_differ`, `hasconfig`.
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct Probed {
    pub legacy: Option<bool>,
    pub resets: Option<bool>,
    pub prefix: Option<PyStr>,
    pub compat_differ: Option<bool>,
    pub hasconfig: Option<bool>,
}

/// `bytes.decode("utf-8", "replace")`, as `run_git` decodes a text
/// answer: each maximal invalid subsequence one U+FFFD.
pub fn text(bytes: &[u8]) -> PyStr {
    String::from_utf8_lossy(bytes)
        .chars()
        .map(u32::from)
        .collect()
}

/// `os.fsdecode(bytes)` of the flavour; Err (UnicodeDecodeError) where
/// Windows' `surrogatepass` refuses the bytes.
pub fn fsdecode(flavour: Flavour, bytes: &[u8]) -> Result<PyStr, Raised> {
    if flavour == Flavour::Posix {
        return Ok(super::record::surrogateescape(bytes));
    }
    let mut out = PyStr::with_capacity(bytes.len());
    let mut rest = bytes;
    loop {
        match std::str::from_utf8(rest) {
            Ok(s) => {
                out.extend(s.chars().map(u32::from));
                return Ok(out);
            }
            Err(e) => {
                let (good, bad) = rest.split_at(e.valid_up_to());
                out.extend(
                    std::str::from_utf8(good)
                        .unwrap_or_default()
                        .chars()
                        .map(u32::from),
                );
                match bad {
                    [0xED, x @ 0xA0..=0xBF, y @ 0x80..=0xBF, ..] => {
                        out.push(0xD000 | (u32::from(x & 0x3F) << 6) | u32::from(y & 0x3F));
                        rest = &bad[3..];
                    }
                    _ => return Err(Raised::UnicodeDecodeError),
                }
            }
        }
    }
}

const BACKSLASH: u32 = b'\\' as u32;
const SLASH: u32 = b'/' as u32;

/// The length of `ntpath.splitdrive(path)[0]`, as CPython 3.13's
/// `splitroot` finds it.
fn nt_drive_len(p: &[u32]) -> usize {
    let norm = |c: u32| if c == SLASH { BACKSLASH } else { c };
    let at = |i: usize| p.get(i).copied().map(norm);
    let find = |from: usize| (from..p.len()).find(|&i| norm(p[i]) == BACKSLASH);
    if at(0) == Some(BACKSLASH) {
        if at(1) != Some(BACKSLASH) {
            return 0;
        }
        // `normp[:8].upper() == "\\\\?\\UNC\\"`.
        let unc: Vec<u32> = "\\\\?\\UNC\\".chars().map(u32::from).collect();
        let head: Option<Vec<u32>> = p
            .get(..8)
            .map(|h| unicode::upper(&h.iter().map(|&c| norm(c)).collect::<Vec<_>>()));
        let start = if head.as_deref() == Some(&unc[..]) {
            8
        } else {
            2
        };
        let Some(index) = find(start) else {
            return p.len();
        };
        let Some(index2) = find(index + 1) else {
            return p.len();
        };
        return index2;
    }
    if at(1) == Some(u32::from(b':')) {
        return 2;
    }
    0
}

/// `_absolute(path, flavour)`: absolute, and on Windows with its drive.
pub fn absolute(flavour: Flavour, path: &[u32]) -> bool {
    match flavour {
        Flavour::Posix => path.first() == Some(&SLASH),
        Flavour::Windows => {
            let drive = nt_drive_len(path);
            drive > 0 && matches!(path.get(drive), Some(&BACKSLASH | &SLASH))
        }
    }
}

/// `_own_files(listing)`: the files a `git config --list --show-scope
/// --show-origin -z` names at system or unknown scope, absolute, each
/// once, the first `OWN_MAX`. Err where Windows' decode raises.
pub fn own_files(flavour: Flavour, listing: &[u8]) -> Result<Vec<PyStr>, Raised> {
    let parts: Vec<&[u8]> = listing.split(|&b| b == 0).collect();
    let mut found: Vec<PyStr> = Vec::new();
    let mut seen: HashSet<PyStr> = HashSet::new();
    let mut i = 0;
    while i + 2 < parts.len() {
        let (scope, origin) = (parts[i], parts[i + 1]);
        i += 3;
        if !matches!(scope, b"system" | b"unknown") || !origin.starts_with(b"file:") {
            continue;
        }
        let path = fsdecode(flavour, &origin[5..])?;
        if absolute(flavour, &path) && seen.insert(path.clone()) {
            // Past `OWN_MAX` nothing more is kept, but each origin is
            // still decoded: Windows' decode may raise on a later one.
            if found.len() < OWN_MAX {
                found.push(path);
            }
        }
    }
    Ok(found)
}

/// The parse inside `_older_system`: the system file named by the first
/// entry of `git config --system --list --show-origin -z` (exit
/// `status`, `stdout`), or `""` where it names none. Err where Windows'
/// decode raises.
pub fn older_system(flavour: Flavour, status: i32, stdout: &[u8]) -> Result<PyStr, Raised> {
    let first: &[u8] = if status == 0 {
        stdout.split(|&b| b == 0).next().unwrap_or_default()
    } else {
        b""
    };
    let Some(rest) = first.strip_prefix(b"file:") else {
        return Ok(PyStr::new());
    };
    let path = fsdecode(flavour, rest)?;
    if absolute(flavour, &path) && path.len() <= MAX_PATH {
        Ok(path)
    } else {
        Ok(PyStr::new())
    }
}

/// `str.rstrip("\r\n")`.
fn rstrip_crlf(s: &[u32]) -> &[u32] {
    let end = s
        .iter()
        .rposition(|&c| c != u32::from(b'\r') && c != u32::from(b'\n'))
        .map_or(0, |i| i + 1);
    &s[..end]
}

/// `str.strip()`.
fn strip(s: &[u32]) -> &[u32] {
    let Some(start) = s.iter().position(|&c| !unicode::is_space(c)) else {
        return &[];
    };
    let end = s
        .iter()
        .rposition(|&c| !unicode::is_space(c))
        .unwrap_or(start)
        + 1;
    &s[start..end]
}

/// `_REMOTE_ANSWERS.get(answer.strip(), (None, None))`: `legacy` and
/// `resets` from what `ls-remote --get-url p` printed.
pub fn remote_answer(answer: &[u32]) -> (Option<bool>, Option<bool>) {
    let key = strip(answer);
    for (name, answer) in [
        ("bettermemory-probe-b", (Some(true), Some(true))),
        ("bettermemory-probe-a", (Some(true), Some(false))),
        ("p", (Some(false), Some(true))),
    ] {
        if key == unicode::cps(name).as_slice() {
            return answer;
        }
    }
    (None, None)
}

/// `_variables`' dict: the names in first-seen order, each with its last
/// value, and an index, so a lookup is a dict's, not a scan.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Variables {
    pub items: Vec<(PyStr, PyStr)>,
    index: HashMap<PyStr, usize>,
}

impl Variables {
    /// `found.get(name)`.
    pub fn get(&self, name: &[u32]) -> Option<&PyStr> {
        self.index.get(name).map(|&i| &self.items[i].1)
    }
}

/// `_variables(listing)`: each NUL-ended `name\nvalue`, the last of a
/// name standing in the first one's place (a dict's order).
pub fn variables(listing: &[u32]) -> Variables {
    let mut found: Vec<(PyStr, PyStr)> = Vec::new();
    let mut index: HashMap<PyStr, usize> = HashMap::new();
    for item in listing.split(|&c| c == 0) {
        if item.is_empty() {
            continue;
        }
        let (name, value) = match item.iter().position(|&c| c == u32::from(b'\n')) {
            Some(i) => (&item[..i], &item[i + 1..]),
            None => (item, &[][..]),
        };
        match index.get(name) {
            Some(&i) => found[i].1 = value.to_vec(),
            None => {
                index.insert(name.to_vec(), found.len());
                found.push((name.to_vec(), value.to_vec()));
            }
        }
    }
    Variables {
        items: found,
        index,
    }
}

/// The answers `_reads_remotes` reads, as the probe's three questions
/// gave them, and `held`: whether the probe stamped and held as before
/// (`_probe_stamps`, `_probe_holds`: unit 3's).
pub fn reads_remotes(remote: Done, prefix: Done, compat: Option<i32>, held: bool) -> Probed {
    let mut said = Vec::with_capacity(2);
    for done in [remote, prefix] {
        match done {
            Some((0, out)) => said.push(rstrip_crlf(&text(out)).to_vec()),
            _ => return Probed::default(),
        }
    }
    if !held {
        return Probed::default();
    }
    let (legacy, resets) = remote_answer(&said[0]);
    let found = variables(&said[1]);
    let get = |name: &str| found.get(&unicode::cps(name));
    let value = get(PREFIX_NAME);
    let tail = unicode::cps(PREFIX_TAIL);
    let prefix = value
        .filter(|v| v.ends_with(&tail))
        .map(|v| v[..v.len() - tail.len()].to_vec());
    let hasconfig = value.map(|_| get(HASCONFIG_NAME).is_some());
    Probed {
        legacy,
        resets,
        prefix,
        compat_differ: compat.map(|status| status == 0),
        hasconfig,
    }
}

/// `gitversion.parse(output)`: the (major, minor, patch) `git version`
/// prints first, each as its decimal text, patch 0 where it names none;
/// None where it names none. Err (ValueError) for a part past `int()`'s
/// 4,300 digits.
pub fn parse_version(output: &[u32]) -> Result<Option<[String; 3]>, Raised> {
    let s = strip(output);
    let lead: Vec<u32> = unicode::cps("git version ");
    let Some(mut rest) = s.strip_prefix(&lead[..]) else {
        return Ok(None);
    };
    let digits = |r: &[u32]| r.iter().take_while(|&&c| unicode::is_decimal(c)).count();
    let dot = u32::from(b'.');
    let n = digits(rest);
    if n == 0 {
        return Ok(None);
    }
    let major = &rest[..n];
    rest = &rest[n..];
    if rest.first() != Some(&dot) {
        return Ok(None);
    }
    rest = &rest[1..];
    let n = digits(rest);
    if n == 0 {
        return Ok(None);
    }
    let minor = &rest[..n];
    rest = &rest[n..];
    let zero = [u32::from(b'0')];
    let patch: &[u32] = match rest.first() {
        Some(&c) if c == dot && digits(&rest[1..]) > 0 => &rest[1..=digits(&rest[1..])],
        _ => &zero,
    };
    let int = |d: &[u32]| match &pyvalue::int_from_str(d) {
        Ok(PyValue::Int(t)) => Ok(t.clone()),
        _ => Err(Raised::ValueError),
    };
    Ok(Some([int(major)?, int(minor)?, int(patch)?]))
}
