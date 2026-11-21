//! `project_file.py`, unit 1: the project file's vocabulary, `mint_id`
//! and `render` (`_toml_string`), and the checks `_from_table` makes of a
//! name (`_bad_name`, `_bad_char`) and of an id (`_ID_RE`). Nothing here
//! reads the machine. `parse` and its reads are unit 2; the walk and
//! `write` are unit 3.
//!
//! `_ID_RE` is matched by hand: `bmp_` and 26 members of `CROCKFORD`, as
//! `checkout_token::token_text` reads them.

use crate::checkout_token::{random_crockford, token_text, Raised as TokenRaised};
use crate::fsdata::Flavour;
use crate::gitfile_rule::hostpath::text::dirname;
use crate::printable::quoted;
use crate::pyvalue::PyStr;

/// `PROJECT_FILE_NAME`.
pub const PROJECT_FILE_NAME: &str = "bettermemory.toml";
/// `ID_PREFIX`.
pub const ID_PREFIX: &str = "bmp_";
/// `ID_LENGTH`.
pub const ID_LENGTH: usize = 26;
/// `_MAX_BYTES`: a longer file is not read (by `_read_bounded`, unit 2).
pub const MAX_BYTES: usize = 64 * 1024;
/// `_QUOTE_LIMIT`: how much of a malformed id a message quotes.
pub const QUOTE_LIMIT: usize = 40;
/// `_SF_DATALESS`.
pub const SF_DATALESS: u32 = 0x4000_0000;
/// `_WINDOWS_RECALL`.
pub const WINDOWS_RECALL: u32 = 0x0004_0000 | 0x0040_0000;

/// `ErrorReason`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorReason {
    Unreadable,
    Evicted,
    NotAFile,
    TooLarge,
    Toml,
    NoProjectTable,
    IdMissing,
    IdMalformed,
    NameMalformed,
    WrongType,
}

impl ErrorReason {
    /// The reason as the reference spells it.
    pub fn name(self) -> &'static str {
        match self {
            ErrorReason::Unreadable => "unreadable",
            ErrorReason::Evicted => "evicted",
            ErrorReason::NotAFile => "not_a_file",
            ErrorReason::TooLarge => "too_large",
            ErrorReason::Toml => "toml",
            ErrorReason::NoProjectTable => "no_project_table",
            ErrorReason::IdMissing => "id_missing",
            ErrorReason::IdMalformed => "id_malformed",
            ErrorReason::NameMalformed => "name_malformed",
            ErrorReason::WrongType => "wrong_type",
        }
    }
}

/// `ProjectFile`; `path` is the text of the reference's `Path`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProjectFile {
    pub id: PyStr,
    pub name: PyStr,
    pub boundary: bool,
    pub path: PyStr,
}

/// `ProjectFileError`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProjectFileError {
    pub path: PyStr,
    pub reason: ErrorReason,
    pub detail: PyStr,
}

impl ProjectFileError {
    /// `line`: the path quoted, then the detail.
    pub fn line(&self) -> PyStr {
        let mut out: PyStr = "bettermemory: ".chars().map(u32::from).collect();
        out.extend(quoted(&self.path));
        out.extend(" names no project: ".chars().map(u32::from));
        out.extend_from_slice(&self.detail);
        out
    }
}

/// `Found`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Found {
    File(ProjectFile),
    Error(ProjectFileError),
}

/// `Above`: `start` and the items, nearest first.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Above {
    pub start: Option<PyStr>,
    pub items: Vec<Found>,
}

/// `ProjectFile.directory` and `ProjectFileError.directory`: the
/// `Path`'s parent: `dirname` of the `Path`'s text, which is already
/// normalised, and `.` where that is empty (one relative component).
pub fn directory(flavour: Flavour, path: &[u32]) -> PyStr {
    let parent = dirname(flavour, path);
    if parent.is_empty() {
        vec![u32::from(b'.')]
    } else {
        parent
    }
}

/// `mint_id()`, the random bytes given.
pub fn mint_id(urandom: &[u8]) -> Result<String, TokenRaised> {
    Ok(format!(
        "{ID_PREFIX}{}",
        random_crockford(ID_LENGTH as i64, urandom)?
    ))
}

/// `_ID_RE.fullmatch(text) is not None`.
pub fn id_text(text: &[u32]) -> bool {
    let prefix: Vec<u32> = ID_PREFIX.chars().map(u32::from).collect();
    text.starts_with(&prefix) && token_text(&text[prefix.len()..])
}

/// `_bad_name(name)`.
pub fn bad_name(name: &[u32]) -> bool {
    name.iter().any(|&c| bad_char(c))
}

/// `_bad_char(char)`.
pub fn bad_char(c: u32) -> bool {
    c < 0x20 || (0x7F..0xA0).contains(&c) || c == 0x2028 || c == 0x2029
}

/// `_toml_string(value)`.
pub fn toml_string(value: &[u32]) -> PyStr {
    let mut out = vec![u32::from(b'"')];
    for &c in value {
        if c == u32::from(b'"') || c == u32::from(b'\\') {
            out.extend([u32::from(b'\\'), c]);
        } else if c < 0x20 || c == 0x7F {
            out.extend(format!("\\u{c:04X}").chars().map(u32::from));
        } else {
            out.push(c);
        }
    }
    out.push(u32::from(b'"'));
    out
}

/// `render(project)`; None where `.encode("utf-8")` raises (a lone
/// surrogate).
pub fn render(project: &ProjectFile) -> Option<Vec<u8>> {
    let mut lines: Vec<PyStr> = [
        "# bettermemory project file, written by `bettermemory project init`.",
        "# It names this folder's project; committed, every clone carries it.",
        "# The id is random: the name can be edited, the id should stay as it is.",
        "[project]",
    ]
    .iter()
    .map(|l| l.chars().map(u32::from).collect())
    .collect();
    for (key, value) in [("id = ", &project.id), ("name = ", &project.name)] {
        let mut line: PyStr = key.chars().map(u32::from).collect();
        line.extend(toml_string(value));
        lines.push(line);
    }
    if project.boundary {
        lines.push("boundary = true".chars().map(u32::from).collect());
    }
    let mut text = lines.join(&u32::from(b'\n'));
    text.push(u32::from(b'\n'));
    text.iter()
        .map(|&c| char::from_u32(c))
        .collect::<Option<String>>()
        .map(String::into_bytes)
}
