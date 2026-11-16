//! The text of one git config file as the reference reads it: the text
//! half of `src/bettermemory/gitconfig.py`.
//!
//! - [`git_entries`]: the lexer and parser (`git_entries`, `_value`,
//!   lines 1675-1786), bytes in, the ordered entries or a plain refusal;
//! - [`Entry::spelled`]: a name split into section, subsection and key
//!   (`_spelled`, lines 389-400);
//! - [`Entry::value_cut`]: a value cut at its first NUL byte, as
//!   `_parse_config` takes it (line 427);
//! - [`spaced`] and [`reads_as_written`]: a value holding a tab or a CR,
//!   which a git before [`KEEPS_SPACES_SINCE`] reads as a space (lines
//!   453-460, and the rule the callers apply);
//! - [`git_int`] and [`git_bool`]: the typed reads (lines 1788-1850);
//! - [`wildmatch`]: git's matcher the `includeIf` conditions use;
//! - [`remote_rules`]: where git's reader of remotes dies on a variable;
//! - [`file`]: one config file read from a file system, bounded;
//! - [`paths`] and [`tree`]: a file and its plain includes.
//!
//! Includes, files, the 2 MiB bound ([`FORMAT_LIMIT`], applied by the
//! readers of files, not by the parser) and the worded reasons a caller
//! gives are the next unit's: an `include` or `includeIf` line is an entry
//! like any other here. Every function runs in time linear in its input.

use std::sync::Arc;

pub mod file;
pub mod paths;
pub mod remote_rules;
pub mod tree;
pub mod wildmatch;

/// The bound the reference's readers of files set on a config file
/// (`_FORMAT_LIMIT`, line 1536); [`git_entries`] itself reads any size.
pub const FORMAT_LIMIT: usize = 1 << 21;

/// The release from which git keeps a tab or a CR inside a value as
/// written (`KEEPS_SPACES_SINCE`, line 453).
pub const KEEPS_SPACES_SINCE: (u32, u32) = (2, 45);

/// One variable as git's callbacks see it (`Entry`, line 1560): its name,
/// lower case but for a subsection and cut at a NUL byte, and its value
/// as written, NUL bytes and all; None for a key with no `=`.
///
/// The section header's part of the name is shared by every entry under
/// it (`stem`), so a long subsection over many keys costs its length once,
/// not once a key; [`Entry::name`] builds the name.
///
/// Equality is the reference's tuple equality, `(name, value)`: two
/// entries whose names are equal once built (`[a "x\0y"]` and
/// `[a "x\0z"]` over one key, both cut to `a.x`) are equal, whatever
/// their stems held past the NUL. The tests compare results with it.
#[derive(Clone, Debug)]
pub struct Entry {
    stem: Arc<[u8]>,
    key: Vec<u8>,
    pub value: Option<Vec<u8>>,
}

/// A name as git's `parse_config_key` splits it (`_spelled`): the section
/// to the first dot, the key from the last, a subsection between them
/// where there are two dots (an empty one is one). Section and key are
/// ASCII; the subsection is kept as bytes (the reference decodes it with
/// `surrogateescape`, which reads back whole).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Name {
    pub section: String,
    pub subsection: Option<Vec<u8>>,
    pub key: String,
}

impl PartialEq for Entry {
    fn eq(&self, other: &Entry) -> bool {
        self.value == other.value && self.name() == other.name()
    }
}

impl Eq for Entry {}

impl Entry {
    /// The name, as the reference's entry holds it: the header's part,
    /// the key lower case, cut at the first NUL byte.
    pub fn name(&self) -> Vec<u8> {
        let mut name = Vec::with_capacity(self.stem.len() + self.key.len());
        name.extend_from_slice(&self.stem);
        name.extend_from_slice(&self.key);
        let cut = cut_at_nul(&name).len();
        name.truncate(cut);
        name
    }

    /// The name split (`_spelled`).
    pub fn spelled(&self) -> Name {
        spelled(&self.name())
    }

    /// The value to its first NUL byte, as `_parse_config` keeps it.
    pub fn value_cut(&self) -> Option<&[u8]> {
        self.value.as_deref().map(cut_at_nul)
    }
}

/// `git_entries` refused the text: git's parse fails ("bad config line")
/// and git dies. The reference gives no reason at this level.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Refused;

/// A git version as data, for [`reads_as_written`]: None where git has
/// not named its version.
pub type GitVersion = Option<(u32, u32, u32)>;

fn cut_at_nul(bytes: &[u8]) -> &[u8] {
    match bytes.iter().position(|&b| b == 0) {
        Some(i) => &bytes[..i],
        None => bytes,
    }
}

/// ASCII decode with `replace`, as the reference decodes a section or a
/// key (always ASCII where the parser made the name).
fn ascii(bytes: &[u8]) -> String {
    bytes
        .iter()
        .map(|&b| if b < 0x80 { b as char } else { '\u{fffd}' })
        .collect()
}

/// `_spelled` of a name's bytes.
pub fn spelled(name: &[u8]) -> Name {
    let (section, rest) = match name.iter().position(|&b| b == b'.') {
        Some(i) => (&name[..i], &name[i + 1..]),
        None => (name, &name[name.len()..]),
    };
    match rest.iter().rposition(|&b| b == b'.') {
        Some(i) => Name {
            section: ascii(section),
            subsection: Some(rest[..i].to_vec()),
            key: ascii(&rest[i + 1..]),
        },
        None => Name {
            section: ascii(section),
            subsection: None,
            key: ascii(rest),
        },
    }
}

/// Whether `value` holds a tab or a CR (`spaced`).
pub fn spaced(value: &[u8]) -> bool {
    value.iter().any(|&b| b == b'\t' || b == b'\r')
}

/// Whether `value` reads as written under the git `version`: one that is
/// not [`spaced`] always does; one that is, only under a git known to be
/// at least [`KEEPS_SPACES_SINCE`] (the rule of `location_git._spellings`,
/// `gitfile_rule.tree_format` and `_include`).
pub fn reads_as_written(value: &[u8], version: GitVersion) -> bool {
    if !spaced(value) {
        return true;
    }
    matches!(version, Some((major, minor, _)) if (major, minor) >= KEEPS_SPACES_SINCE)
}

const BOM: &[u8] = b"\xef\xbb\xbf";

fn is_trim(c: u8) -> bool {
    c == b' ' || c == b'\t' || c == b'\r'
}

fn is_key_char(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'-'
}

/// `data` with each CR LF written LF, left to right, as `bytes.replace`.
fn crlf_to_lf(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len());
    let mut i = 0;
    while i < data.len() {
        if data[i] == b'\r' && data.get(i + 1) == Some(&b'\n') {
            out.push(b'\n');
            i += 2;
        } else {
            out.push(data[i]);
            i += 1;
        }
    }
    out
}

/// The end of the blank space and comments at `pos` (`_BETWEEN`).
fn between(text: &[u8], mut pos: usize) -> usize {
    while pos < text.len() {
        match text[pos] {
            b' ' | b'\t' | b'\n' | b'\r' => pos += 1,
            b'#' | b';' => {
                while pos < text.len() && text[pos] != b'\n' {
                    pos += 1;
                }
            }
            _ => break,
        }
    }
    pos
}

/// A section header at `pos` (its `[`): the stem it sets and the position
/// past it (`_HEADER`, `_SUB_ESCAPE`), or None where it does not match or
/// is closed with no name.
fn header(text: &[u8], pos: usize) -> Option<(Vec<u8>, usize)> {
    let end = text.len();
    let mut i = pos + 1;
    let start = i;
    while i < end && (is_key_char(text[i]) || text[i] == b'.') {
        i += 1;
    }
    let name = &text[start..i];
    let mut stem: Vec<u8> = name.to_ascii_lowercase();
    stem.push(b'.');
    if i < end && text[i] == b']' {
        if name.is_empty() {
            return None;
        }
        return Some((stem, i + 1));
    }
    let blank = i;
    while i < end && is_trim(text[i]) {
        i += 1;
    }
    if i == blank || i >= end || text[i] != b'"' {
        return None;
    }
    i += 1;
    loop {
        if i >= end {
            return None;
        }
        match text[i] {
            b'"' => break,
            b'\n' => return None,
            b'\\' => {
                if i + 1 >= end || text[i + 1] == b'\n' {
                    return None;
                }
                stem.push(text[i + 1]);
                i += 2;
            }
            c => {
                stem.push(c);
                i += 1;
            }
        }
    }
    // At the closing quote: `]` must follow.
    if i + 1 >= end || text[i + 1] != b']' {
        return None;
    }
    stem.push(b'.');
    Some((stem, i + 2))
}

fn escape(c: u8) -> Option<u8> {
    match c {
        b't' => Some(b'\t'),
        b'b' => Some(0x08),
        b'n' => Some(b'\n'),
        b'\\' => Some(b'\\'),
        b'"' => Some(b'"'),
        _ => None,
    }
}

/// The value that starts at `pos` and the position past the newline
/// ending it (`_value`), or None where git refuses it.
fn value(text: &[u8], mut pos: usize) -> Option<(Vec<u8>, usize)> {
    let end = text.len();
    // `_PLAIN`: no backslash and no quote before the comment or newline.
    let mut i = pos;
    while i < end && !matches!(text[i], b'\n' | b'\\' | b'"' | b'#' | b';') {
        i += 1;
    }
    let run_end = i;
    if i < end && (text[i] == b'#' || text[i] == b';') {
        while i < end && text[i] != b'\n' {
            i += 1;
        }
    }
    if i >= end || text[i] == b'\n' {
        let mut run = &text[pos..run_end];
        while let [first, rest @ ..] = run {
            if is_trim(*first) {
                run = rest;
            } else {
                break;
            }
        }
        while let [rest @ .., last] = run {
            if is_trim(*last) {
                run = rest;
            } else {
                break;
            }
        }
        return Some((run.to_vec(), if i >= end { end } else { i + 1 }));
    }
    let mut out = Vec::new();
    let mut quote = false;
    let mut comment = false;
    let mut trim = 0usize;
    loop {
        let mut c = if pos < end { text[pos] } else { b'\n' };
        pos += 1;
        if c == b'\n' {
            if quote {
                return None;
            }
            if trim != 0 {
                out.truncate(trim);
            }
            return Some((out, pos));
        }
        if comment {
            continue;
        }
        if is_trim(c) && !quote {
            if trim == 0 {
                trim = out.len();
            }
            if !out.is_empty() {
                out.push(c);
            }
            continue;
        }
        if !quote && (c == b'#' || c == b';') {
            comment = true;
            continue;
        }
        trim = 0;
        if c == b'\\' {
            c = if pos < end { text[pos] } else { b'\n' };
            pos += 1;
            if c == b'\n' {
                continue;
            }
            out.push(escape(c)?);
        } else if c == b'"' {
            quote = !quote;
        } else {
            out.push(c);
        }
    }
}

/// The variables the config text `data` sets, in order, as git's own
/// parser reads them (`git_entries`), or [`Refused`] where git dies.
pub fn git_entries(data: &[u8]) -> Result<Vec<Entry>, Refused> {
    let data = if data.starts_with(BOM) {
        &data[BOM.len()..]
    } else if data.first() == Some(&BOM[0]) {
        return Err(Refused);
    } else {
        data
    };
    let text = crlf_to_lf(data);
    let end = text.len();
    let mut out = Vec::new();
    let mut stem: Arc<[u8]> = Arc::from(&b""[..]);
    let mut pos = 0usize;
    loop {
        pos = between(&text, pos.min(end));
        if pos >= end {
            return Ok(out);
        }
        if text[pos] == b'[' {
            let (next, past) = header(&text, pos).ok_or(Refused)?;
            stem = Arc::from(next);
            pos = past;
            continue;
        }
        if !text[pos].is_ascii_alphabetic() {
            return Err(Refused);
        }
        let start = pos;
        while pos < end && is_key_char(text[pos]) {
            pos += 1;
        }
        let key = &text[start..pos];
        while pos < end && (text[pos] == b' ' || text[pos] == b'\t') {
            pos += 1;
        }
        let mut found = None;
        if pos < end && text[pos] == b'=' {
            let (v, past) = value(&text, pos + 1).ok_or(Refused)?;
            found = Some(v);
            pos = past;
        } else if pos < end && text[pos] != b'\n' {
            return Err(Refused);
        } else {
            pos += 1;
        }
        out.push(Entry {
            stem: Arc::clone(&stem),
            key: key.to_ascii_lowercase(),
            value: found,
        });
    }
}

const INT_MAX: i128 = (1 << 31) - 1;
const INTMAX_MAX: i128 = (1 << 63) - 1;

/// The int git reads from a config value (`git_int`: `strtoimax` in base
/// 0, then a unit `k`, `m` or `g` in either case, within an int), or None
/// where git dies on it, as on no value.
pub fn git_int(value: Option<&[u8]>) -> Option<i64> {
    let text = cut_at_nul(value?);
    let n = text.len();
    let mut i = 0;
    while i < n && matches!(text[i], b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r') {
        i += 1;
    }
    let mut negative = false;
    if i < n && (text[i] == b'+' || text[i] == b'-') {
        negative = text[i] == b'-';
        i += 1;
    }
    let hexed = i + 2 < n
        && text[i] == b'0'
        && (text[i + 1] | 0x20) == b'x'
        && text[i + 2].is_ascii_hexdigit();
    let (base, start): (u32, usize) = if hexed {
        (16, i + 2)
    } else if text.get(i) == Some(&b'0') {
        (8, i)
    } else {
        (10, i)
    };
    let mut after = start;
    // Past INTMAX_MAX + 1 the number is out of range whatever follows; the
    // accumulator stops growing there so it cannot overflow.
    let mut magnitude: i128 = 0;
    while after < n && (text[after] as char).is_digit(base) {
        let digit = (text[after] as char).to_digit(base).unwrap_or(0) as i128;
        if magnitude <= INTMAX_MAX + 1 {
            magnitude = magnitude * base as i128 + digit;
        }
        after += 1;
    }
    if after == start {
        return None;
    }
    let number = if negative { -magnitude } else { magnitude };
    let factor: i128 = match text[after..].to_ascii_lowercase().as_slice() {
        b"" => 1,
        b"k" => 1 << 10,
        b"m" => 1 << 20,
        b"g" => 1 << 30,
        _ => return None,
    };
    if !(-INTMAX_MAX - 1..=INTMAX_MAX).contains(&number) {
        return None;
    }
    if (number < 0 && -(INT_MAX / factor) > number) || (number > 0 && INT_MAX / factor < number) {
        return None;
    }
    i64::try_from(number * factor).ok()
}

/// The boolean git reads from a config value (`git_bool`): true with no
/// value, false where empty, the words in any case, else an int that is
/// not zero; None where git dies on it.
pub fn git_bool(value: Option<&[u8]>) -> Option<bool> {
    let Some(value) = value else {
        return Some(true);
    };
    let text = cut_at_nul(value);
    if text.is_empty() {
        return Some(false);
    }
    match text.to_ascii_lowercase().as_slice() {
        b"true" | b"yes" | b"on" => Some(true),
        b"false" | b"no" | b"off" => Some(false),
        _ => git_int(Some(text)).map(|n| n != 0),
    }
}
