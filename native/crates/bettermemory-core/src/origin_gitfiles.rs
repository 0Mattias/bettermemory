//! The repository files `origin` reads in-process: the port of
//! `src/bettermemory/origin_gitfiles.py` (see the README's "The repository
//! files origin reads in-process").
//!
//! - this file, unit 1: the pure decoders, none of which reads the
//!   machine: `_unquote_c_path`, `_quoted_header_path`,
//!   `_dequote_named_header`, `_dequote_patch_headers` (with
//!   `claims.hunk_lines`), `_decode_varint`, `_gitmodules_entries` and
//!   `_shortening_refs`. Built.
//! - [`machine`], unit 2: the leaves that read the machine through the
//!   `Fs` trait: `_read_prefix`, `relative_to_root` and `_spelled_as_git`.
//!   Built.
//! - [`facts`], unit 3: what git dies on in a repository's or the
//!   machine's config (`_rev_parse_dies` and its tables,
//!   `_machine_config_dies`), what a config text says (`_facts_of`,
//!   `_config_entries`), the directories a check holds
//!   (`_hold_directory`, `directories_held`) and the global attributes
//!   file (`_global_attributes_path`). Built.
//! - [`root`], unit 4: the per-stamp memo of config facts
//!   (`_config_facts`), whether a repository's config may move its
//!   working tree (`_config_moves_worktree`), the root read from the files
//!   where `git rev-parse` exited non-zero (`refused_worktree_root`) and
//!   the once-per-path memo (`first_not_a_directory`). Built.
//! - [`attrs`], unit 6: the attribute file signatures
//!   (`attribute_files_signature`, `gitattributes_signature`), the
//!   index's `.gitmodules` entry with its memo (`_index_gitmodules`) and
//!   `_gitdir_at_root`. Built.
//! - [`walk`], unit 7 with unit 4's remainder: the origin cache's
//!   signature (`capture_files`), the files beside the history
//!   (`walk_files_signature`), the head `locate` reads (`located_head`)
//!   and the check at the end of a search (`files_held`). Built.
//!
//! Texts are Python `str` values as code points ([`PyStr`]), since a
//! caller's text may hold a lone surrogate, which the reference's UTF-8
//! encode refuses with `UnicodeEncodeError`.

pub mod attrs;
pub mod facts;
pub mod machine;
pub mod root;
pub mod walk;

use crate::pyvalue::PyStr;

/// An exception the reference raises out of a text decoder.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Raised {
    /// `str.encode("utf-8")` of a lone surrogate.
    UnicodeEncodeError,
    /// `int` of a hunk count past the 4,300-digit limit.
    ValueError,
}

impl Raised {
    pub fn name(self) -> &'static str {
        match self {
            Raised::UnicodeEncodeError => "UnicodeEncodeError",
            Raised::ValueError => "ValueError",
        }
    }
}

const QUOTE: u32 = b'"' as u32;
const BACKSLASH: u32 = b'\\' as u32;
const LF: u32 = b'\n' as u32;
const CR: u32 = b'\r' as u32;

fn starts(text: &[u32], prefix: &str) -> bool {
    let mut at = text.iter();
    prefix.chars().all(|c| at.next() == Some(&u32::from(c)))
}

fn cat(prefix: &str, rest: &[u32]) -> PyStr {
    let mut out: PyStr = prefix.chars().map(u32::from).collect();
    out.extend_from_slice(rest);
    out
}

/// `_C_QUOTE_ESCAPES`: git's single-character escapes and their bytes.
fn c_escape(cp: u32) -> Option<u8> {
    Some(match char::from_u32(cp)? {
        'a' => 0x07,
        'b' => 0x08,
        't' => 0x09,
        'n' => 0x0A,
        'v' => 0x0B,
        'f' => 0x0C,
        'r' => 0x0D,
        '"' => 0x22,
        '\\' => 0x5C,
        _ => return None,
    })
}

fn is_octal(cp: u32) -> bool {
    (u32::from(b'0')..=u32::from(b'7')).contains(&cp)
}

/// `_unquote_c_path`: one complete git C-quoted string decoded, or None
/// where `quoted` is not exactly that.
pub fn unquote_c_path(quoted: &[u32]) -> Result<Option<PyStr>, Raised> {
    if quoted.len() < 2 || quoted[0] != QUOTE || quoted[quoted.len() - 1] != QUOTE {
        return Ok(None);
    }
    let body = &quoted[1..quoted.len() - 1];
    let mut out: Vec<u8> = Vec::new();
    let n = body.len();
    let mut i = 0;
    while i < n {
        let ch = body[i];
        if ch == QUOTE {
            return Ok(None);
        }
        if ch != BACKSLASH {
            let c = char::from_u32(ch).ok_or(Raised::UnicodeEncodeError)?;
            let mut buf = [0u8; 4];
            out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            i += 1;
            continue;
        }
        i += 1;
        if i >= n {
            return Ok(None);
        }
        let esc = body[i];
        if let Some(code) = c_escape(esc) {
            out.push(code);
            i += 1;
            continue;
        }
        if is_octal(esc) {
            let mut j = i + 1;
            while j < n && j - i < 3 && is_octal(body[j]) {
                j += 1;
            }
            let value = body[i..j]
                .iter()
                .fold(0u32, |v, &d| v * 8 + (d - u32::from(b'0')));
            out.push((value & 0xFF) as u8);
            i = j;
            continue;
        }
        return Ok(None);
    }
    Ok(std::str::from_utf8(&out)
        .ok()
        .map(|s| s.chars().map(u32::from).collect()))
}

/// `_quoted_header_path`: the decoded path, or None where it is not one
/// complete quoted string or names a line break.
pub fn quoted_header_path(quoted: &[u32]) -> Result<Option<PyStr>, Raised> {
    Ok(unquote_c_path(quoted)?.filter(|d| !d.contains(&LF) && !d.contains(&CR)))
}

/// `_NAMING_HEADERS`.
const NAMING_HEADERS: [&str; 4] = ["rename from ", "rename to ", "copy from ", "copy to "];

/// `_dequote_named_header`: a `diff --git` line of two quoted sides
/// `a/P` and `b/P`, or a rename or copy line of one, unquoted; None for
/// any other line.
pub fn dequote_named_header(line: &[u32]) -> Result<Option<PyStr>, Raised> {
    if starts(line, "diff --git \"") {
        let rest = &line[11..];
        // `range(1, len(rest) - 2)`.
        let mut k = 1;
        while k + 2 < rest.len() {
            if rest[k] == QUOTE && rest[k + 1] == u32::from(b' ') && rest[k + 2] == QUOTE {
                let old = quoted_header_path(&rest[..=k])?;
                let new = quoted_header_path(&rest[k + 2..])?;
                if let (Some(old), Some(new)) = (old, new) {
                    if starts(&old, "a/") && new == cat("b/", &old[2..]) {
                        let mut out = cat("diff --git ", &old);
                        out.push(u32::from(b' '));
                        out.extend_from_slice(&new);
                        return Ok(Some(out));
                    }
                }
            }
            k += 1;
        }
        return Ok(None);
    }
    for prefix in NAMING_HEADERS {
        let width = prefix.len();
        if starts(line, prefix) && line.get(width) == Some(&QUOTE) {
            return Ok(quoted_header_path(&line[width..])?.map(|d| cat(prefix, &d)));
        }
    }
    Ok(None)
}

/// `claims.hunk_lines`: the lines of a `-U0` hunk whose header is
/// `line`, as decimal text (Python's `int` has no bound), or None where
/// it is no hunk header; ValueError past `int`'s 4,300 digits. The
/// reference imports it from `claims`, and so does the port.
pub fn hunk_lines(line: &[u32]) -> Result<Option<String>, Raised> {
    crate::claims::hunk_lines(line).map_err(|_| Raised::ValueError)
}

/// `_QUOTED_HEADER_MARKS`.
const MARKS: [&str; 5] = ["--- \"", "+++ \"", "diff --git \"", " from \"", " to \""];

fn contains(text: &[u32], mark: &str) -> bool {
    let mark: Vec<u32> = mark.chars().map(u32::from).collect();
    text.windows(mark.len()).any(|w| w == mark.as_slice())
}

/// `_dequote_patch_headers`: the stream with its residually quoted
/// headers unquoted, a hunk's lines read past by their count.
pub fn dequote_patch_headers(stream: &[u32]) -> Result<PyStr, Raised> {
    if !MARKS.iter().any(|m| contains(stream, m)) {
        return Ok(stream.to_vec());
    }
    let mut lines: Vec<PyStr> = stream.split(|&c| c == LF).map(<[u32]>::to_vec).collect();
    let mut left: u64 = 0;
    for line in &mut lines {
        if left > 0 {
            if line.first() == Some(&BACKSLASH) {
                continue;
            }
            if starts(line, "-") || starts(line, "+") {
                left -= 1;
                continue;
            }
            left = 0;
        }
        if let Some(size) = hunk_lines(line)? {
            // A count past any stream's line count reads as a larger one.
            left = size.parse::<u64>().unwrap_or(u64::MAX);
            continue;
        }
        if let Some(named) = dequote_named_header(line)? {
            *line = named;
            continue;
        }
        if !(starts(line, "--- \"") || starts(line, "+++ \"")) {
            continue;
        }
        let mut quoted = &line[4..];
        if quoted.len() >= 2 && quoted.ends_with(&[QUOTE, u32::from(b'\t')]) {
            quoted = &quoted[..quoted.len() - 1];
        }
        let Some(decoded) = quoted_header_path(quoted)? else {
            continue;
        };
        let prefix = if starts(line, "--- ") { "a/" } else { "b/" };
        if starts(&decoded, prefix) {
            let mut out = line[..4].to_vec();
            out.extend_from_slice(&decoded);
            *line = out;
        }
    }
    Ok(lines.join(&LF))
}

/// The reference's two index exceptions.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IndexFault {
    /// `_IndexUnsettled`: not an index read exactly.
    Unsettled,
    /// `_IndexShort`: the prefix read ends before the scan does.
    Short,
}

impl IndexFault {
    pub fn name(self) -> &'static str {
        match self {
            IndexFault::Unsettled => "_IndexUnsettled",
            IndexFault::Short => "_IndexShort",
        }
    }

    fn ends(complete: bool) -> IndexFault {
        if complete {
            IndexFault::Unsettled
        } else {
            IndexFault::Short
        }
    }
}

/// `_decode_varint`: git's `decode_varint`, the value at `pos` and the
/// position after it.
pub fn decode_varint(data: &[u8], pos: usize, complete: bool) -> Result<(u64, usize), IndexFault> {
    let ends = IndexFault::ends(complete);
    let mut pos = pos;
    let mut byte = *data.get(pos).ok_or(ends)?;
    pos += 1;
    // Below 2**57 before each shift, so below 2**64 after it; the `+ 1`
    // can reach 2**64, which u128 holds as Python's int does.
    let mut value = u128::from(byte & 0x7F);
    while byte & 0x80 != 0 {
        value += 1;
        if value >> 57 != 0 {
            return Err(IndexFault::Unsettled);
        }
        byte = *data.get(pos).ok_or(ends)?;
        pos += 1;
        value = (value << 7) + u128::from(byte & 0x7F);
    }
    Ok((value as u64, pos))
}

const GITMODULES: &[u8] = b".gitmodules";
/// `_EXTENDED_FLAGS`.
const EXTENDED_FLAGS: u16 = 0x4000 | 0x2000;

/// One `.gitmodules` entry: stage, mode, object id.
pub type GitmodulesEntry = (u8, u32, Vec<u8>);

fn be16(data: &[u8], at: usize) -> u16 {
    u16::from_be_bytes([data[at], data[at + 1]])
}

fn be32(data: &[u8], at: usize) -> u32 {
    u32::from_be_bytes([data[at], data[at + 1], data[at + 2], data[at + 3]])
}

fn find_nul(data: &[u8], from: usize) -> Option<usize> {
    data.get(from..)?
        .iter()
        .position(|&b| b == 0)
        .map(|p| p + from)
}

/// `_gitmodules_entries`: each entry named `.gitmodules` in the index
/// `data`, read up to the first name that sorts after it.
pub fn gitmodules_entries(data: &[u8], complete: bool) -> Result<Vec<GitmodulesEntry>, IndexFault> {
    let ends = IndexFault::ends(complete);
    if data.len() < 12 {
        return Err(ends);
    }
    if &data[..4] != b"DIRC" {
        return Err(IndexFault::Unsettled);
    }
    let version = be32(data, 4);
    if !(2..=4).contains(&version) {
        return Err(IndexFault::Unsettled);
    }
    let entries = be32(data, 8);
    let mut found = Vec::new();
    let mut pos = 12usize;
    let mut previous: Vec<u8> = Vec::new();
    for _ in 0..entries {
        let start = pos;
        let flags_at = start + 60;
        if flags_at + 2 > data.len() {
            return Err(ends);
        }
        let mode = be32(data, start + 24);
        let oid = data[start + 40..flags_at].to_vec();
        let flags = be16(data, flags_at);
        let mut name_at = flags_at + 2;
        if flags & 0x4000 != 0 {
            if version < 3 {
                return Err(IndexFault::Unsettled);
            }
            if name_at + 2 > data.len() {
                return Err(ends);
            }
            if be16(data, name_at) & !EXTENDED_FLAGS != 0 {
                return Err(IndexFault::Unsettled);
            }
            name_at += 2;
        }
        let stage = ((flags >> 12) & 0x3) as u8;
        let mut length = usize::from(flags & 0x0FFF);
        let name: Vec<u8>;
        if version == 4 {
            let (strip, at) = decode_varint(data, name_at, complete)?;
            name_at = at;
            if strip > previous.len() as u64 {
                return Err(IndexFault::Unsettled);
            }
            let end = find_nul(data, name_at).ok_or(ends)?;
            let keep = previous.len() - strip as usize;
            name = [&previous[..keep], &data[name_at..end]].concat();
            if length != 0x0FFF && name.len() != length {
                return Err(IndexFault::Unsettled);
            }
            pos = end + 1;
        } else {
            if length == 0x0FFF {
                length = find_nul(data, name_at).ok_or(ends)? - name_at;
            }
            let stop = (name_at + length).min(data.len());
            name = data[name_at.min(stop)..stop].to_vec();
            if name.len() != length {
                return Err(ends);
            }
            pos = start + ((name_at - start + length + 8) & !7);
        }
        if name == GITMODULES {
            found.push((stage, mode, oid));
        } else if name.as_slice() > GITMODULES {
            break;
        }
        previous = name;
    }
    Ok(found)
}

/// `_REV_PARSE_RULES`, each as its prefix and suffix around `{}`.
const REV_PARSE_RULES: [(&str, &str); 6] = [
    ("", ""),
    ("refs/", ""),
    ("refs/tags/", ""),
    ("refs/heads/", ""),
    ("refs/remotes/", ""),
    ("refs/remotes/", "/HEAD"),
];

/// `_shortening_refs`: the refs whose existence decides how `git
/// symbolic-ref --short HEAD` shortens `refname`, first seen first.
pub fn shortening_refs(refname: &[u32]) -> Vec<PyStr> {
    let mut names: Vec<PyStr> = Vec::new();
    for index in (1..REV_PARSE_RULES.len()).rev() {
        let (prefix, suffix) = REV_PARSE_RULES[index];
        let (p, s) = (prefix.len(), suffix.len());
        let suffixed = refname.len() >= s && starts(&refname[refname.len() - s..], suffix);
        if !(starts(refname, prefix) && suffixed && refname.len() > p + s) {
            continue;
        }
        let short = &refname[p..refname.len() - s];
        for (other, (rp, rs)) in REV_PARSE_RULES.iter().enumerate() {
            if other != index {
                let mut name = cat(rp, short);
                name.extend(rs.chars().map(u32::from));
                if !names.contains(&name) {
                    names.push(name);
                }
            }
        }
    }
    names
}
