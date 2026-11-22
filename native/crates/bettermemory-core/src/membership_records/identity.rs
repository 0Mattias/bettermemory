//! The portable identity (`membership_records.py` 110-120, 195-203 and
//! 890-997): `_strings`, `_flag`, `_FULL_SHA`, `PortableIdentity` with
//! `to_json`, `from_json` and `digest`, and `ImportedProject`. Unit 4 of
//! the module's cut (membership_records-3b).

use std::collections::BTreeSet;

use sha2::{Digest, Sha256};

use crate::claims::strip;
use crate::json::{dumps, DumpOptions};
use crate::pyvalue::{repr_str, PyStr, PyValue};
use crate::unicode;

/// `WITNESS_BOUND`: the most witnesses an identity carries (D15).
pub const WITNESS_BOUND: usize = 64;
/// `KEEP_OLDEST`, `BLOBS_PER_WITNESS`, `WALK_LIMIT` (design 2.2).
pub const KEEP_OLDEST: usize = 16;
pub const BLOBS_PER_WITNESS: usize = 8;
pub const WALK_LIMIT: usize = 512;
/// `EXPORT_WITNESSES` and the import's question kinds and format words.
pub const EXPORT_WITNESSES: usize = 16;
pub const IMPORT_ONE_WITNESS: &str = "import_one_witness";
pub const IMPORT_CONTRADICTION: &str = "import_contradiction";
pub const IMPORT_SEVERAL: &str = "import_several_projects";
pub const EXPORT_FORMAT: &str = "bettermemory membership export";
pub const EXPORT_FORMAT_VERSION: i64 = 1;

/// `_FULL_SHA.fullmatch(text)`, `[0-9a-f]{40}(?:[0-9a-f]{24})?` matched by
/// hand: exactly 40 or 64 code points, each an ASCII digit or `a` to `f`.
/// With no flags the classes are ASCII ranges and the pattern has no
/// other reading.
pub fn full_sha(text: &[u32]) -> bool {
    (text.len() == 40 || text.len() == 64)
        && text
            .iter()
            .all(|&c| (0x30..=0x39).contains(&c) || (0x61..=0x66).contains(&c))
}

fn ascii(text: &str) -> PyStr {
    unicode::cps(text)
}

/// The ValueError `from_json` raises: its text.
pub type Malformed = PyStr;

fn malformed(text: String) -> Malformed {
    ascii(&text)
}

/// `_strings(raw, key)`: a list of non-empty `str`, or the ValueError.
/// A missing key reads as an empty list.
pub fn strings(raw: &PyValue, key: &str) -> Result<Vec<PyStr>, Malformed> {
    let wrong = || malformed(format!("a project identity's '{key}' is a list of strings"));
    match raw.get(key) {
        None => Ok(Vec::new()),
        Some(PyValue::List(items)) => items
            .iter()
            .map(|v| match v {
                PyValue::Str(s) if !s.is_empty() => Ok(s.clone()),
                _ => Err(wrong()),
            })
            .collect(),
        Some(_) => Err(wrong()),
    }
}

/// `_flag(raw, key)`: a bool, False where missing, or the ValueError.
pub fn flag(raw: &PyValue, key: &str) -> Result<bool, Malformed> {
    match raw.get(key) {
        None => Ok(false),
        Some(PyValue::Bool(b)) => Ok(*b),
        Some(_) => Err(malformed(format!(
            "a project identity's '{key}' is true or false"
        ))),
    }
}

/// `PortableIdentity`. `parent` is a Python int as canonical decimal text
/// (any size).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PortableIdentity {
    pub name: PyStr,
    pub file_ids: Vec<PyStr>,
    pub witnesses: Vec<PyStr>,
    pub remotes: Vec<PyStr>,
    pub folder: bool,
    pub boundary: bool,
    pub parent: Option<String>,
}

impl PortableIdentity {
    /// The identity with the reference's defaults: only a name.
    pub fn named(name: PyStr) -> Self {
        PortableIdentity {
            name,
            file_ids: Vec::new(),
            witnesses: Vec::new(),
            remotes: Vec::new(),
            folder: false,
            boundary: false,
            parent: None,
        }
    }

    /// `to_json`: a dict in the reference's key order.
    pub fn to_json(&self) -> PyValue {
        let list = |v: &[PyStr]| PyValue::List(v.iter().cloned().map(PyValue::Str).collect());
        PyValue::Dict(vec![
            (PyValue::str("name"), PyValue::Str(self.name.clone())),
            (PyValue::str("file_ids"), list(&self.file_ids)),
            (PyValue::str("witnesses"), list(&self.witnesses)),
            (PyValue::str("remotes"), list(&self.remotes)),
            (PyValue::str("folder"), PyValue::Bool(self.folder)),
            (PyValue::str("boundary"), PyValue::Bool(self.boundary)),
            (
                PyValue::str("parent"),
                match &self.parent {
                    None => PyValue::None,
                    Some(t) => PyValue::Int(t.clone()),
                },
            ),
        ])
    }

    /// `from_json(raw)`: the identity, or the ValueError's text. The
    /// checks run in the reference's order: the mapping, the name, the
    /// parent, the remotes' list, the witnesses' list and each witness
    /// after `strip().lower()`, then the file ids and the two flags.
    pub fn from_json(raw: &PyValue) -> Result<Self, Malformed> {
        if !matches!(raw, PyValue::Dict(_)) {
            return Err(ascii("a project identity is an object"));
        }
        let name = match raw.get("name") {
            Some(PyValue::Str(s)) if !s.is_empty() => s.clone(),
            _ => return Err(ascii("a project identity names the project")),
        };
        let parent = match raw.get("parent") {
            None | Some(PyValue::None) => None,
            Some(PyValue::Int(t)) if !t.starts_with('-') => Some(t.clone()),
            Some(_) => return Err(ascii("a project identity's parent is a position or null")),
        };
        let local = ascii("local:");
        let remotes: BTreeSet<PyStr> = strings(raw, "remotes")?
            .into_iter()
            .filter(|k| !k.starts_with(&local))
            .collect();
        let witnesses: Vec<PyStr> = strings(raw, "witnesses")?
            .iter()
            .map(|w| unicode::lower(strip(w)))
            .collect();
        if let Some(bad) = witnesses.iter().find(|w| !full_sha(w)) {
            let shown = repr_str(&bad[..bad.len().min(72)]);
            let mut text = ascii("a project identity's witness ");
            text.extend(shown);
            text.extend(ascii(" is not a full commit id"));
            return Err(text);
        }
        let file_ids: BTreeSet<PyStr> = strings(raw, "file_ids")?.into_iter().collect();
        let mut seen = BTreeSet::new();
        let mut kept = Vec::new();
        for w in witnesses {
            if seen.insert(w.clone()) {
                kept.push(w);
            }
        }
        kept.truncate(WITNESS_BOUND);
        Ok(PortableIdentity {
            name,
            file_ids: file_ids.into_iter().collect(),
            witnesses: kept,
            remotes: remotes.into_iter().collect(),
            folder: flag(raw, "folder")?,
            boundary: flag(raw, "boundary")?,
            parent,
        })
    }

    /// `digest()`: the first 24 hex digits of the SHA-256 of
    /// `json.dumps` (keys sorted, ASCII) of the name, the sorted file ids
    /// and remotes, the witnesses in order and the two flags.
    pub fn digest(&self) -> String {
        let sorted = |v: &[PyStr]| {
            let mut v = v.to_vec();
            v.sort();
            PyValue::List(v.into_iter().map(PyValue::Str).collect())
        };
        let body = PyValue::Dict(vec![
            (PyValue::str("name"), PyValue::Str(self.name.clone())),
            (PyValue::str("file_ids"), sorted(&self.file_ids)),
            (
                PyValue::str("witnesses"),
                PyValue::List(self.witnesses.iter().cloned().map(PyValue::Str).collect()),
            ),
            (PyValue::str("remotes"), sorted(&self.remotes)),
            (PyValue::str("folder"), PyValue::Bool(self.folder)),
            (PyValue::str("boundary"), PyValue::Bool(self.boundary)),
        ]);
        let options = DumpOptions {
            sort_keys: true,
            ..DumpOptions::DEFAULT
        };
        // ensure_ascii: every code point is ASCII, so UTF-8 is the bytes.
        let text = dumps(&body, options).expect("strings, lists and bools always encode");
        let bytes: Vec<u8> = text.iter().map(|&c| c as u8).collect();
        let hex: String = Sha256::digest(&bytes)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        hex[..24].to_string()
    }
}

/// `ImportedProject`: how an import mapped one identity. `codes` keeps
/// the insertion order of the reference's dict.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ImportedProject {
    pub project: PyStr,
    pub rule: PyStr,
    pub codes: Vec<(PyStr, Vec<PyStr>)>,
    pub proposals: Vec<PyStr>,
}
