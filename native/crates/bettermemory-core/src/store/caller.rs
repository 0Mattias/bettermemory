//! What a caller is admitted to see, given how it was resolved: the
//! reference's `membership_records.relation` and `admitted` (design 3.5's
//! fixed table, `IN_PROJECT_POLICY` and `OUTSIDE_POLICY` over `SURFACES`),
//! `admission.Admission` (`nothing`, `project_home`, `admits_project`,
//! `admits_row`, `admitted_projects`, `admits_global`) and the reads of the
//! membership model they make (`Membership.canonical`, `chain_of`,
//! `named`, `project`, `projects`, over `membership_rows("projects")`: the
//! ``projects`` table with the ``project`` rows of
//! ``native_membership_rows`` replacing the legacy rows they key).
//!
//! Pure: the caller is a [`Resolution`] (data, parsed from the JSON the
//! reference's dataclass gives), the tables are read through a
//! connection, and nothing is written. [`Admission::pool_admission`] gives
//! the [`pool::Admission`] the native search consumes.
//!
//! Values are kept as SQLite gave them ([`Val`]) and compared as Python
//! compares them, so a hostile table (a cycle, a missing row, a value of
//! another type, a NULL) gives the reference's answer; a read the
//! reference raises on gives [`ModelError::Raised`] with the exception's
//! class. A native row whose JSON is bytes is read as `json.loads` reads
//! bytes: `json.detect_encoding`, then the decode with ``surrogatepass``;
//! bytes that do not decode make the row malformed. Every read is linear
//! in the rows (keys are hashed as Python's dict files them).

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};

use rusqlite::types::ValueRef;
use rusqlite::Connection;

use super::pool;
use crate::json::{self, DumpOptions};
use crate::pyvalue::{self, PyStr, PyValue};
use crate::{float_repr, unicode};

/// `membership_records.GLOBAL`.
pub const GLOBAL: &str = "global";
/// `admission.NO_RESOLUTION`.
pub const NO_RESOLUTION: &str = "no_resolution";
/// `membership_records.UNRESOLVED_STATES`.
pub const UNRESOLVED_STATES: [&str; 2] = ["indeterminate", "deferred"];
/// `membership_records.PROJECT_SCOPE`.
pub const PROJECT_SCOPE: &str = "projects:";

/// `membership_records.SURFACES`, in its order.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Surface {
    SessionBrief,
    PromptRecall,
    ReviewStrata,
    SearchAutoScope,
    PoolPrefilter,
    Links,
    SearchUnscoped,
}

impl Surface {
    pub const ALL: [Surface; 7] = [
        Surface::SessionBrief,
        Surface::PromptRecall,
        Surface::ReviewStrata,
        Surface::SearchAutoScope,
        Surface::PoolPrefilter,
        Surface::Links,
        Surface::SearchUnscoped,
    ];

    pub fn name(self) -> &'static str {
        match self {
            Surface::SessionBrief => "session_brief",
            Surface::PromptRecall => "prompt_recall",
            Surface::ReviewStrata => "review_strata",
            Surface::SearchAutoScope => "search_auto_scope",
            Surface::PoolPrefilter => "pool_prefilter",
            Surface::Links => "links",
            Surface::SearchUnscoped => "search_unscoped",
        }
    }

    /// The surface a name names; None where the reference raises
    /// ValueError (``unknown surface``).
    pub fn parse(name: &str) -> Option<Surface> {
        Surface::ALL.into_iter().find(|s| s.name() == name)
    }

    /// `IN_PROJECT_POLICY[surface]`: what it admits for a caller in a
    /// project.
    pub fn in_project(self, rel: Relation) -> bool {
        use Relation::*;
        match self {
            Surface::SessionBrief | Surface::PromptRecall | Surface::ReviewStrata => {
                matches!(rel, SameProject | GlobalNamed)
            }
            Surface::SearchAutoScope | Surface::PoolPrefilter | Surface::Links => {
                matches!(rel, SameProject | SubProject | Global | GlobalNamed)
            }
            Surface::SearchUnscoped => {
                matches!(
                    rel,
                    SameProject | SubProject | Global | GlobalNamed | OtherProject
                )
            }
        }
    }

    /// `OUTSIDE_POLICY[surface]`: what it admits for a caller outside any
    /// project (D3).
    pub fn outside(self, rel: Relation) -> bool {
        use Relation::*;
        match self {
            Surface::SessionBrief | Surface::PromptRecall | Surface::ReviewStrata => false,
            Surface::SearchAutoScope | Surface::PoolPrefilter | Surface::Links => rel == Global,
            Surface::SearchUnscoped => matches!(rel, Global | OtherProject),
        }
    }
}

/// The relations of design 3.5 (`membership_records.relation`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Relation {
    Unknown,
    GlobalNamed,
    Global,
    OtherTenant,
    SameProject,
    SubProject,
    OtherProject,
}

impl Relation {
    pub fn name(self) -> &'static str {
        match self {
            Relation::Unknown => "unknown",
            Relation::GlobalNamed => "global_named",
            Relation::Global => "global",
            Relation::OtherTenant => "other_tenant",
            Relation::SameProject => "same_project",
            Relation::SubProject => "sub_project",
            Relation::OtherProject => "other_project",
        }
    }
}

/// Why the membership model could not answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ModelError {
    /// The reference raises this exception class reading the projects
    /// (OperationalError for a missing table or column or text that is
    /// not UTF-8, TypeError for ids JSON or `<` cannot take).
    Raised(&'static str),
    /// What the port does not read (none of the reads here gives it
    /// now; kept for the callers that match on it).
    NotPorted(&'static str),
}

impl ModelError {
    pub fn name(&self) -> &'static str {
        match self {
            ModelError::Raised(name) | ModelError::NotPorted(name) => name,
        }
    }
}

const OPERATIONAL: ModelError = ModelError::Raised("OperationalError");
const TYPE_ERROR: ModelError = ModelError::Raised("TypeError");

/// A value as the membership tables hand it to Python: SQLite's five,
/// and a native row's JSON `true`/`false`.
#[derive(Clone, Debug)]
pub enum Val {
    Null,
    Bool(bool),
    /// Canonical decimal text (a native row's JSON integer is unbounded).
    Int(String),
    Real(f64),
    Text(PyStr),
    Blob(Vec<u8>),
}

fn cps(text: &str) -> PyStr {
    unicode::cps(text)
}

impl Val {
    pub fn text(text: &str) -> Val {
        Val::Text(cps(text))
    }

    /// Python's truth value.
    pub fn truthy(&self) -> bool {
        match self {
            Val::Null => false,
            Val::Bool(b) => *b,
            Val::Int(t) => t != "0",
            Val::Real(f) => *f != 0.0,
            Val::Text(t) => !t.is_empty(),
            Val::Blob(b) => !b.is_empty(),
        }
    }

    fn number(&self) -> Option<PyValue> {
        match self {
            Val::Bool(b) => Some(PyValue::Bool(*b)),
            Val::Int(t) => Some(PyValue::Int(t.clone())),
            Val::Real(f) => Some(PyValue::Float(*f)),
            _ => None,
        }
    }

    /// Python's `==`.
    pub fn py_eq(&self, other: &Val) -> bool {
        match (self, other) {
            (Val::Null, Val::Null) => true,
            (Val::Text(a), Val::Text(b)) => a == b,
            (Val::Blob(a), Val::Blob(b)) => a == b,
            _ => match (self.number(), other.number()) {
                (Some(a), Some(b)) => pyvalue::cmp_keys(&a, &b) == Some(Ordering::Equal),
                _ => false,
            },
        }
    }

    /// The key a Python dict files this value under: equal exactly when
    /// Python's `==` holds (an int, an integral float and a bool of one
    /// value share a key; SQLite holds no NaN).
    fn dict_key(&self) -> DictKey {
        match self {
            Val::Null => DictKey::Null,
            Val::Text(t) => DictKey::Text(t.clone()),
            Val::Blob(b) => DictKey::Blob(b.clone()),
            Val::Bool(b) => DictKey::Int(if *b { "1" } else { "0" }.into()),
            Val::Int(t) => DictKey::Int(t.clone()),
            Val::Real(f) if f.fract() == 0.0 && f.is_finite() => DictKey::Int(integer_text(*f)),
            Val::Real(f) => DictKey::Real((*f).to_bits()),
        }
    }

    /// Whether this is the `str` `text`.
    pub fn is_text(&self, text: &[u32]) -> bool {
        matches!(self, Val::Text(t) if t == text)
    }

    /// `str(value)`.
    pub fn py_str(&self) -> PyStr {
        match self {
            Val::Null => cps("None"),
            Val::Bool(true) => cps("True"),
            Val::Bool(false) => cps("False"),
            Val::Int(t) => cps(t),
            Val::Real(f) => cps(&float_repr::repr(*f)),
            Val::Text(t) => t.clone(),
            Val::Blob(b) => cps(&bytes_repr(b)),
        }
    }

    fn from_ref(v: ValueRef<'_>) -> Result<Val, ModelError> {
        Ok(match v {
            ValueRef::Null => Val::Null,
            ValueRef::Integer(i) => Val::Int(i.to_string()),
            ValueRef::Real(f) => Val::Real(f),
            // Python's sqlite3 raises OperationalError ("Could not decode
            // to UTF-8") for text that is not UTF-8.
            ValueRef::Text(b) => Val::Text(cps(std::str::from_utf8(b).map_err(|_| OPERATIONAL)?)),
            ValueRef::Blob(b) => Val::Blob(b.to_vec()),
        })
    }

    fn from_json(v: &PyValue) -> Option<Val> {
        Some(match v {
            PyValue::None => Val::Null,
            PyValue::Bool(b) => Val::Bool(*b),
            PyValue::Int(t) => Val::Int(t.clone()),
            PyValue::Str(s) => Val::Text(s.clone()),
            _ => return None,
        })
    }
}

/// A dict key, hashed: see [`Val::dict_key`].
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum DictKey {
    Null,
    Text(PyStr),
    Blob(Vec<u8>),
    /// Canonical decimal text.
    Int(String),
    /// A float that is not integral, by its bits.
    Real(u64),
}

/// The exact decimal text of an integral finite float (`-0.0` is `0`).
fn integer_text(f: f64) -> String {
    if f == 0.0 {
        return "0".into();
    }
    format!("{f:.0}")
}

/// `repr(bytes)`.
pub fn bytes_repr(b: &[u8]) -> String {
    let quote = if b.contains(&b'\'') && !b.contains(&b'"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from("b");
    out.push(quote);
    for &c in b {
        match c {
            b'\\' => out.push_str("\\\\"),
            b'\t' => out.push_str("\\t"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            c if c as char == quote => {
                out.push('\\');
                out.push(quote);
            }
            0x20..=0x7e => out.push(c as char),
            c => out.push_str(&format!("\\x{c:02x}")),
        }
    }
    out.push(quote);
    out
}

/// One project row as `_project_from` reads it (the columns admission
/// reads): ``id`` and ``name`` through `str()`, the rest as stored.
#[derive(Clone, Debug)]
pub struct Project {
    pub id: PyStr,
    pub name: PyStr,
    pub merged_into: Val,
    pub provisional: Val,
    pub parent: Val,
    pub tenant: Val,
}

/// A row before `str()`: what `membership_rows` returns.
#[derive(Clone, Debug)]
struct RawRow {
    id: Val,
    name: Val,
    merged_into: Val,
    provisional: Val,
    parent: Val,
    tenant: Val,
}

/// The projects table's columns, in order (`_MembershipTable`).
const COLUMNS: [&str; 9] = [
    "id",
    "name",
    "merged_into",
    "provisional",
    "parent",
    "tenant",
    "folder",
    "boundary",
    "minted",
];

/// `{str(id): project}` in the dict's order (a later row of one id
/// replaces the earlier in its place).
#[derive(Clone, Debug, Default)]
struct Projects {
    rows: Vec<Project>,
    index: HashMap<PyStr, usize>,
}

impl Projects {
    fn get(&self, id: &[u32]) -> Option<&Project> {
        self.index.get(id).map(|&i| &self.rows[i])
    }

    fn insert(&mut self, p: Project) {
        match self.index.get(&p.id) {
            Some(&i) => self.rows[i] = p,
            None => {
                self.index.insert(p.id.clone(), self.rows.len());
                self.rows.push(p);
            }
        }
    }
}

/// The membership model's projects, read once.
#[derive(Clone, Debug)]
pub struct Membership {
    read: Result<Projects, ModelError>,
}

impl Membership {
    /// `Membership._all_projects` over `conn`: the error the reference's
    /// read raises is kept and returned by each answer that reads.
    pub fn read(conn: &Connection) -> Membership {
        Membership::from(read_projects(conn))
    }

    /// A model over project rows given as data.
    pub fn from_projects(rows: Vec<Project>) -> Membership {
        let mut projects = Projects::default();
        for p in rows {
            projects.insert(p);
        }
        Membership::from(Ok(projects))
    }

    fn from(read: Result<Projects, ModelError>) -> Membership {
        Membership { read }
    }

    fn all(&self) -> Result<&Projects, ModelError> {
        self.read.as_ref().map_err(Clone::clone)
    }

    fn lookup(&self, id: &Val) -> Option<&Project> {
        match (id, &self.read) {
            (Val::Text(t), Ok(p)) => p.get(t),
            _ => None,
        }
    }

    /// The error the read raised, if any.
    pub fn error(&self) -> Option<&ModelError> {
        self.read.as_ref().err()
    }

    /// Every row, in the dict's order.
    pub fn all_projects(&self) -> Result<&[Project], ModelError> {
        Ok(&self.all()?.rows)
    }

    /// `Membership.project`: the row as recorded; None for KeyError.
    pub fn project(&self, id: &[u32]) -> Result<Option<&Project>, ModelError> {
        Ok(self.all()?.get(id))
    }

    /// `Membership.projects`: every project not merged into another, by id.
    pub fn projects(&self) -> Result<Vec<&Project>, ModelError> {
        let mut out: Vec<&Project> = self
            .all()?
            .rows
            .iter()
            .filter(|p| matches!(p.merged_into, Val::Null))
            .collect();
        out.sort_by(|a, b| a.id.cmp(&b.id));
        Ok(out)
    }

    /// `Membership.canonical`: the project `id` stands for now, merges
    /// followed; a cycle stops where it would revisit a project.
    pub fn canonical(&self, id: &Val) -> Result<Val, ModelError> {
        self.all()?;
        let global = cps(GLOBAL);
        let mut seen: HashSet<PyStr> = HashSet::new();
        let mut cur = id.clone();
        loop {
            if cur.is_text(&global) {
                return Ok(cur);
            }
            let Some(p) = self.lookup(&cur) else {
                return Ok(cur);
            };
            if matches!(p.merged_into, Val::Null) || seen.contains(&p.id) {
                return Ok(cur);
            }
            seen.insert(p.id.clone());
            cur = p.merged_into.clone();
        }
    }

    /// `Membership.chain_of`: the project and its parents, nearest first.
    pub fn chain_of(&self, id: &Val) -> Result<Vec<PyStr>, ModelError> {
        let global = cps(GLOBAL);
        let mut out: Vec<PyStr> = Vec::new();
        let mut in_out: HashSet<PyStr> = HashSet::new();
        let mut pid = self.canonical(id)?;
        loop {
            if pid.is_text(&global) {
                return Ok(out);
            }
            let Some(p) = self.lookup(&pid) else {
                return Ok(out);
            };
            if in_out.contains(&p.id) {
                return Ok(out);
            }
            in_out.insert(p.id.clone());
            out.push(p.id.clone());
            pid = self.canonical(&p.parent)?;
        }
    }

    /// `Membership.named`: the projects (merges followed) a record's
    /// ``projects:<name>`` scopes name, names compared case-folded.
    pub fn named(&self, scopes: &[PyStr]) -> Result<Vec<Val>, ModelError> {
        let prefix = cps(PROJECT_SCOPE);
        let wanted: HashSet<PyStr> = scopes
            .iter()
            .filter(|s| s.starts_with(&prefix) && s.len() > prefix.len())
            .map(|s| unicode::casefold(&s[prefix.len()..]))
            .collect();
        if wanted.is_empty() {
            return Ok(Vec::new());
        }
        let mut out: Vec<Val> = Vec::new();
        let mut seen: HashSet<DictKey> = HashSet::new();
        for p in &self.all()?.rows {
            if wanted.contains(&unicode::casefold(&p.name)) {
                let c = self.canonical(&Val::Text(p.id.clone()))?;
                if seen.insert(c.dict_key()) {
                    out.push(c);
                }
            }
        }
        Ok(out)
    }
}

fn sql(e: rusqlite::Error) -> ModelError {
    let _ = e;
    OPERATIONAL
}

/// `store_native.native_rows` for the projects: each key's row, or None
/// for a row that does not read, in the dict's order.
fn native_rows(conn: &Connection) -> Result<Vec<(Val, Option<RawRow>)>, ModelError> {
    let mut statement = match conn
        .prepare("SELECT key_text, row_text FROM native_membership_rows WHERE member_kind = ?")
    {
        Ok(s) => s,
        Err(rusqlite::Error::SqliteFailure(_, Some(m)))
            if m == "no such table: native_membership_rows" =>
        {
            return Ok(Vec::new());
        }
        Err(e) => return Err(sql(e)),
    };
    let mut rows = statement.query(["project"]).map_err(sql)?;
    let mut found: Vec<(Val, Option<RawRow>)> = Vec::new();
    let mut slots: HashMap<DictKey, usize> = HashMap::new();
    while let Some(row) = rows.next().map_err(sql)? {
        let key = Val::from_ref(row.get_ref(0).map_err(sql)?)?;
        let text = Val::from_ref(row.get_ref(1).map_err(sql)?)?;
        let parsed = native_row(&key, &text)?;
        match slots.get(&key.dict_key()) {
            Some(&i) => found[i].1 = parsed,
            None => {
                slots.insert(key.dict_key(), found.len());
                found.push((key, parsed));
            }
        }
    }
    Ok(found)
}

/// `json.detect_encoding`: the codec `json.loads` decodes bytes with.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Codec {
    Utf8,
    Utf8Sig,
    /// With a BOM, which sets the order and is consumed.
    Utf16,
    Utf16Be,
    Utf16Le,
    Utf32,
    Utf32Be,
    Utf32Le,
}

fn detect_encoding(b: &[u8]) -> Codec {
    if b.starts_with(&[0, 0, 0xfe, 0xff]) || b.starts_with(&[0xff, 0xfe, 0, 0]) {
        return Codec::Utf32;
    }
    if b.starts_with(&[0xfe, 0xff]) || b.starts_with(&[0xff, 0xfe]) {
        return Codec::Utf16;
    }
    if b.starts_with(&[0xef, 0xbb, 0xbf]) {
        return Codec::Utf8Sig;
    }
    if b.len() >= 4 {
        if b[0] == 0 {
            return if b[1] != 0 {
                Codec::Utf16Be
            } else {
                Codec::Utf32Be
            };
        }
        if b[1] == 0 {
            return if b[2] != 0 || b[3] != 0 {
                Codec::Utf16Le
            } else {
                Codec::Utf32Le
            };
        }
    } else if b.len() == 2 {
        if b[0] == 0 {
            return Codec::Utf16Be;
        }
        if b[1] == 0 {
            return Codec::Utf16Le;
        }
    }
    Codec::Utf8
}

/// UTF-8 with ``surrogatepass``: a surrogate's three-byte spelling is
/// taken as that code point; any other invalid byte fails.
fn utf8_surrogatepass(mut b: &[u8]) -> Option<PyStr> {
    let mut out = PyStr::new();
    loop {
        match std::str::from_utf8(b) {
            Ok(s) => {
                out.extend(s.chars().map(|c| c as u32));
                return Some(out);
            }
            Err(e) => {
                let (good, rest) = b.split_at(e.valid_up_to());
                out.extend(
                    std::str::from_utf8(good)
                        .unwrap_or_default()
                        .chars()
                        .map(|c| c as u32),
                );
                match rest {
                    [0xed, x @ 0xa0..=0xbf, y @ 0x80..=0xbf, ..] => {
                        out.push(0xd000 | (u32::from(x & 0x3f) << 6) | u32::from(y & 0x3f));
                        b = &rest[3..];
                    }
                    _ => return None,
                }
            }
        }
    }
}

/// UTF-16 with ``surrogatepass``: pairs combine, a lone surrogate is
/// kept, a trailing odd byte fails.
fn utf16_surrogatepass(b: &[u8], big: bool) -> Option<PyStr> {
    if !b.len().is_multiple_of(2) {
        return None;
    }
    let units: Vec<u32> = b
        .chunks_exact(2)
        .map(|c| {
            u32::from(if big {
                u16::from_be_bytes([c[0], c[1]])
            } else {
                u16::from_le_bytes([c[0], c[1]])
            })
        })
        .collect();
    let mut out = PyStr::with_capacity(units.len());
    let mut i = 0;
    while i < units.len() {
        let u = units[i];
        if (0xd800..0xdc00).contains(&u) {
            if let Some(&low) = units.get(i + 1) {
                if (0xdc00..0xe000).contains(&low) {
                    out.push(0x10000 + ((u - 0xd800) << 10) + (low - 0xdc00));
                    i += 2;
                    continue;
                }
            }
        }
        out.push(u);
        i += 1;
    }
    Some(out)
}

/// UTF-32 with ``surrogatepass``: a surrogate code point is kept, one
/// past U+10FFFF or a length not a multiple of four fails.
fn utf32_surrogatepass(b: &[u8], big: bool) -> Option<PyStr> {
    if !b.len().is_multiple_of(4) {
        return None;
    }
    b.chunks_exact(4)
        .map(|c| {
            let a = [c[0], c[1], c[2], c[3]];
            let u = if big {
                u32::from_be_bytes(a)
            } else {
                u32::from_le_bytes(a)
            };
            (u <= 0x10ffff).then_some(u)
        })
        .collect()
}

/// `bytes.decode(json.detect_encoding(b), "surrogatepass")`, as
/// `json.loads` reads bytes; None for UnicodeDecodeError (a ValueError
/// the read catches: the row is malformed).
pub(crate) fn json_bytes(b: &[u8]) -> Option<PyStr> {
    match detect_encoding(b) {
        Codec::Utf8 => utf8_surrogatepass(b),
        Codec::Utf8Sig => utf8_surrogatepass(&b[3..]),
        Codec::Utf16 => utf16_surrogatepass(&b[2..], b[0] == 0xfe),
        Codec::Utf16Be => utf16_surrogatepass(b, true),
        Codec::Utf16Le => utf16_surrogatepass(b, false),
        Codec::Utf32 => utf32_surrogatepass(&b[4..], b[0] == 0),
        Codec::Utf32Be => utf32_surrogatepass(b, true),
        Codec::Utf32Le => utf32_surrogatepass(b, false),
    }
}

/// One native row: `json.loads`, `_membership_values`, and its key
/// compared with `key`; None where the reference catches the failure.
fn native_row(key: &Val, text: &Val) -> Result<Option<RawRow>, ModelError> {
    let text = match text {
        Val::Text(t) => t.clone(),
        Val::Blob(b) => match json_bytes(b) {
            Some(t) => t,
            None => return Ok(None),
        },
        _ => return Ok(None),
    };
    let Ok(parsed) = json::loads(&text) else {
        return Ok(None);
    };
    let PyValue::Dict(items) = &parsed else {
        return Ok(None);
    };
    let names: Vec<PyStr> = COLUMNS.iter().map(|c| cps(c)).collect();
    if items
        .iter()
        .any(|(k, _)| !matches!(k, PyValue::Str(s) if names.contains(s)))
    {
        return Ok(None);
    }
    let mut values: Vec<Val> = Vec::with_capacity(COLUMNS.len());
    for name in &names {
        let Some((_, v)) = items
            .iter()
            .rev()
            .find(|(k, _)| matches!(k, PyValue::Str(s) if s == name))
        else {
            return Ok(None);
        };
        match Val::from_json(v) {
            Some(v) => values.push(v),
            None => return Ok(None),
        }
    }
    let Val::Text(id) = &values[0] else {
        return Ok(None);
    };
    if id.is_empty() {
        return Ok(None);
    }
    let Ok(row_key) = json::dumps(
        &PyValue::List(vec![PyValue::Str(id.clone())]),
        DumpOptions::SORTED_ASCII_COMPACT,
    ) else {
        return Ok(None);
    };
    if !key.is_text(&row_key) {
        return Ok(None);
    }
    let mut v = values.into_iter();
    let mut next = || v.next().unwrap_or(Val::Null);
    Ok(Some(RawRow {
        id: next(),
        name: next(),
        merged_into: next(),
        provisional: next(),
        parent: next(),
        tenant: next(),
    }))
}

/// `store_native.row_key` of a legacy row: TypeError for an id
/// `json.dumps` refuses (bytes).
fn legacy_key(id: &Val) -> Result<PyStr, ModelError> {
    let value = match id {
        Val::Null => PyValue::None,
        Val::Bool(b) => PyValue::Bool(*b),
        Val::Int(t) => PyValue::Int(t.clone()),
        Val::Real(f) => PyValue::Float(*f),
        Val::Text(t) => PyValue::Str(t.clone()),
        Val::Blob(_) => return Err(TYPE_ERROR),
    };
    json::dumps(
        &PyValue::List(vec![value]),
        DumpOptions::SORTED_ASCII_COMPACT,
    )
    .map_err(|e| ModelError::Raised(e.name()))
}

/// `store_rows._sort_rows(rows, ["id"])`'s order of two ids: None first,
/// numbers by value, text by code point; Err for a pair `<` refuses.
fn id_order(a: &Val, b: &Val) -> Result<Ordering, ModelError> {
    match (a, b) {
        (Val::Null, Val::Null) => Ok(Ordering::Equal),
        (Val::Null, _) => Ok(Ordering::Less),
        (_, Val::Null) => Ok(Ordering::Greater),
        (Val::Text(x), Val::Text(y)) => Ok(x.cmp(y)),
        (Val::Blob(x), Val::Blob(y)) => Ok(x.cmp(y)),
        _ => match (a.number(), b.number()) {
            (Some(x), Some(y)) => pyvalue::cmp_keys(&x, &y).ok_or(TYPE_ERROR),
            _ => Err(TYPE_ERROR),
        },
    }
}

/// `membership_rows(conn, "projects")` read into `_all_projects`' dict.
fn read_projects(conn: &Connection) -> Result<Projects, ModelError> {
    let native = native_rows(conn)?;
    let mut statement = conn
        .prepare(&format!(
            "SELECT {} FROM projects ORDER BY id",
            COLUMNS.join(", ")
        ))
        .map_err(sql)?;
    let mut rows = statement.query([]).map_err(sql)?;
    let mut out: Vec<RawRow> = Vec::new();
    while let Some(row) = rows.next().map_err(sql)? {
        let mut cells = Vec::with_capacity(COLUMNS.len());
        for i in 0..COLUMNS.len() {
            cells.push(Val::from_ref(row.get_ref(i).map_err(sql)?)?);
        }
        let mut v = cells.into_iter();
        let mut next = || v.next().unwrap_or(Val::Null);
        out.push(RawRow {
            id: next(),
            name: next(),
            merged_into: next(),
            provisional: next(),
            parent: next(),
            tenant: next(),
        });
    }
    if !native.is_empty() {
        let keys: HashSet<DictKey> = native.iter().map(|(k, _)| k.dict_key()).collect();
        let mut kept = Vec::with_capacity(out.len());
        for row in out {
            let key = Val::Text(legacy_key(&row.id)?);
            if !keys.contains(&key.dict_key()) {
                kept.push(row);
            }
        }
        kept.extend(native.into_iter().filter_map(|(_, row)| row));
        // A sort that completes compares every pair adjacent in its
        // result, so it raises exactly when two ids `<` refuses are both
        // present.
        let classes: HashSet<u8> = kept
            .iter()
            .filter_map(|r| match r.id {
                Val::Null => None,
                Val::Text(_) => Some(0),
                Val::Blob(_) => Some(1),
                _ => Some(2),
            })
            .collect();
        if classes.len() > 1 {
            return Err(TYPE_ERROR);
        }
        let mut failed = None;
        kept.sort_by(|a, b| {
            id_order(&a.id, &b.id).unwrap_or_else(|e| {
                failed = Some(e);
                Ordering::Equal
            })
        });
        if let Some(e) = failed {
            return Err(e);
        }
        out = kept;
    }
    let mut projects = Projects::default();
    for r in out {
        projects.insert(Project {
            id: r.id.py_str(),
            name: r.name.py_str(),
            merged_into: r.merged_into,
            provisional: r.provisional,
            parent: r.parent,
            tenant: r.tenant,
        });
    }
    Ok(projects)
}

/// `membership_records.Resolution`: the outcome of the ladder for one
/// location.
#[derive(Clone, Debug, PartialEq)]
pub struct Resolution {
    pub outcome: PyStr,
    pub project: Option<PyStr>,
    pub checkout: Option<PyStr>,
    pub rule: PyStr,
    pub proposal: Option<PyStr>,
    pub candidates: Vec<PyStr>,
    pub witnesses: Vec<(PyStr, Vec<PyStr>)>,
    pub chain: Vec<PyStr>,
    pub tenant: Option<PyStr>,
    pub lines: Vec<PyStr>,
    pub rows: Vec<String>,
    pub remint_token: bool,
}

/// A resolution's JSON that does not make a `Resolution`: the field.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResolutionError(pub String);

fn field_str(v: &PyValue, name: &str) -> Result<PyStr, ResolutionError> {
    match v {
        PyValue::Str(s) => Ok(s.clone()),
        _ => Err(ResolutionError(name.into())),
    }
}

fn field_opt(v: Option<&PyValue>, name: &str) -> Result<Option<PyStr>, ResolutionError> {
    match v {
        None | Some(PyValue::None) => Ok(None),
        Some(v) => field_str(v, name).map(Some),
    }
}

fn field_list(v: Option<&PyValue>, name: &str) -> Result<Vec<PyStr>, ResolutionError> {
    match v {
        None => Ok(Vec::new()),
        Some(PyValue::List(items)) | Some(PyValue::Tuple(items)) => {
            items.iter().map(|i| field_str(i, name)).collect()
        }
        Some(_) => Err(ResolutionError(name.into())),
    }
}

impl Resolution {
    /// The resolution `dataclasses.asdict` writes as JSON.
    pub fn from_json(text: &str) -> Result<Resolution, ResolutionError> {
        let v = json::loads(&cps(text)).map_err(|e| ResolutionError(e.name().into()))?;
        Resolution::from_value(&v)
    }

    pub fn from_value(v: &PyValue) -> Result<Resolution, ResolutionError> {
        let PyValue::Dict(items) = v else {
            return Err(ResolutionError("not an object".into()));
        };
        const KNOWN: [&str; 12] = [
            "outcome",
            "project",
            "checkout",
            "rule",
            "proposal",
            "candidates",
            "witnesses",
            "chain",
            "tenant",
            "lines",
            "rows",
            "remint_token",
        ];
        let get = |name: &str| {
            let key = cps(name);
            items
                .iter()
                .rev()
                .find(|(k, _)| matches!(k, PyValue::Str(s) if *s == key))
                .map(|(_, v)| v)
        };
        for (k, _) in items {
            let known = matches!(k, PyValue::Str(s) if KNOWN.iter().any(|n| cps(n) == *s));
            if !known {
                return Err(ResolutionError("unknown field".into()));
            }
        }
        let required = |name: &str| get(name).ok_or_else(|| ResolutionError(name.into()));
        let project = match required("project")? {
            PyValue::None => None,
            v => Some(field_str(v, "project")?),
        };
        let checkout = match required("checkout")? {
            PyValue::None => None,
            v => Some(field_str(v, "checkout")?),
        };
        let witnesses = match get("witnesses") {
            None => Vec::new(),
            Some(PyValue::Dict(pairs)) => pairs
                .iter()
                .map(|(k, v)| {
                    Ok((
                        field_str(k, "witnesses")?,
                        field_list(Some(v), "witnesses")?,
                    ))
                })
                .collect::<Result<_, ResolutionError>>()?,
            Some(_) => return Err(ResolutionError("witnesses".into())),
        };
        let rows = match get("rows") {
            None => Vec::new(),
            Some(PyValue::List(items)) => items
                .iter()
                .map(|i| match i {
                    PyValue::Int(t) => Ok(t.clone()),
                    _ => Err(ResolutionError("rows".into())),
                })
                .collect::<Result<_, _>>()?,
            Some(_) => return Err(ResolutionError("rows".into())),
        };
        let remint_token = match get("remint_token") {
            None => false,
            Some(PyValue::Bool(b)) => *b,
            Some(_) => return Err(ResolutionError("remint_token".into())),
        };
        Ok(Resolution {
            outcome: field_str(required("outcome")?, "outcome")?,
            project,
            checkout,
            rule: field_str(required("rule")?, "rule")?,
            proposal: field_opt(get("proposal"), "proposal")?,
            candidates: field_list(get("candidates"), "candidates")?,
            witnesses,
            chain: field_list(get("chain"), "chain")?,
            tenant: field_opt(get("tenant"), "tenant")?,
            lines: field_list(get("lines"), "lines")?,
            rows,
            remint_token,
        })
    }
}

/// A record's home as `relation` reads it (`membership_records.Home`).
#[derive(Clone, Debug)]
pub struct Home {
    /// The project it stands for now (merges followed).
    pub project: Val,
    pub chain: Vec<PyStr>,
    pub tenant: Val,
    /// The provisional project of writes made where nothing could be read
    /// (decision B6).
    pub unresolved: bool,
    /// For a global record, the projects its scopes name.
    pub named: Vec<Val>,
}

/// `membership_records.relation`.
pub fn relation(home: &Home, caller: &Resolution) -> Relation {
    let global = cps(GLOBAL);
    let Some(project) = &caller.project else {
        return Relation::Unknown;
    };
    if home.project.is_text(&global) {
        if *project != global {
            let own = [project.clone()];
            let chain: &[PyStr] = if caller.chain.is_empty() {
                &own
            } else {
                &caller.chain
            };
            if home
                .named
                .iter()
                .any(|n| chain.iter().any(|c| n.is_text(c)))
            {
                return Relation::GlobalNamed;
            }
        }
        return Relation::Global;
    }
    let caller_tenant = caller.tenant.as_ref().filter(|t| !t.is_empty());
    let differ = match (home.tenant.truthy(), caller_tenant) {
        (false, None) => false,
        (true, Some(t)) => !home.tenant.is_text(t),
        _ => true,
    };
    if differ {
        return Relation::OtherTenant;
    }
    if *project == global {
        return Relation::OtherProject;
    }
    if caller.chain.iter().any(|c| home.project.is_text(c)) {
        return Relation::SameProject;
    }
    if home.chain.contains(project) {
        return Relation::SubProject;
    }
    Relation::OtherProject
}

/// `membership_records.admitted`: whether `surface` may deliver a record
/// with `home` to `caller`.
pub fn admitted(surface: Surface, home: &Home, caller: &Resolution) -> bool {
    let rel = relation(home, caller);
    if rel == Relation::Unknown {
        return false;
    }
    if home.unresolved && rel != Relation::SameProject {
        return false;
    }
    if caller.project.as_deref() == Some(&cps(GLOBAL)[..]) {
        return surface.outside(rel);
    }
    surface.in_project(rel)
}

/// `admission.Admission`: one surface's admission for one caller (None:
/// its resolution raised) over one membership model.
#[derive(Clone, Copy, Debug)]
pub struct Admission<'a> {
    pub surface: Surface,
    pub caller: Option<&'a Resolution>,
    pub model: &'a Membership,
}

impl<'a> Admission<'a> {
    pub fn new(surface: Surface, caller: Option<&'a Resolution>, model: &'a Membership) -> Self {
        Admission {
            surface,
            caller,
            model,
        }
    }

    /// `Admission.nothing`: the caller's outcome when it is admitted
    /// nothing on any surface; None otherwise.
    pub fn nothing(&self) -> Option<PyStr> {
        let Some(c) = self.caller else {
            return Some(cps(NO_RESOLUTION));
        };
        if UNRESOLVED_STATES.iter().any(|s| cps(s) == c.outcome) || c.project.is_none() {
            return Some(c.outcome.clone());
        }
        None
    }

    /// `Admission.project_home`: the home a record whose ``homes`` row
    /// names `recorded` has.
    pub fn project_home(&self, recorded: &[u32]) -> Result<Home, ModelError> {
        let mut home = Home {
            project: Val::Text(recorded.to_vec()),
            chain: Vec::new(),
            tenant: Val::Null,
            unresolved: false,
            named: Vec::new(),
        };
        if recorded == &cps(GLOBAL)[..] {
            return Ok(home);
        }
        let found = self.model.canonical(&home.project)?;
        if found.truthy() {
            home.project = found;
        }
        home.chain = self.model.chain_of(&home.project)?;
        if let Some(known) = self.model.lookup(&home.project) {
            home.tenant = known.tenant.clone();
            home.unresolved = UNRESOLVED_STATES
                .iter()
                .any(|s| known.provisional.is_text(&cps(s)));
        }
        Ok(home)
    }

    /// `Admission.admits`.
    pub fn admits(&self, home: &Home) -> bool {
        match self.caller {
            Some(c) => admitted(self.surface, home, c),
            None => false,
        }
    }

    /// `Admission.admits_project`: None is a record with no home row.
    pub fn admits_project(&self, recorded: Option<&[u32]>) -> Result<bool, ModelError> {
        match recorded {
            None => Ok(false),
            Some(r) => Ok(self.admits(&self.project_home(r)?)),
        }
    }

    /// `Admission.admits_row`: a global record is decided with the
    /// projects its scopes name.
    pub fn admits_row(
        &self,
        scopes: &[PyStr],
        recorded: Option<&[u32]>,
    ) -> Result<bool, ModelError> {
        let global = cps(GLOBAL);
        if recorded != Some(&global[..]) {
            return self.admits_project(recorded);
        }
        let named = self.model.named(scopes)?;
        if named.is_empty() {
            return self.admits_project(recorded);
        }
        let mut home = self.project_home(&global)?;
        home.named = named;
        Ok(self.admits(&home))
    }

    /// `Admission.admitted_projects`: None when the caller is admitted
    /// nothing; else the projects not merged away it admits, by id.
    pub fn admitted_projects(&self) -> Result<Option<Vec<PyStr>>, ModelError> {
        if self.nothing().is_some() {
            return Ok(None);
        }
        let mut out = Vec::new();
        for p in self.model.projects()? {
            if self.admits_project(Some(&p.id))? {
                out.push(p.id.clone());
            }
        }
        Ok(Some(out))
    }

    /// `Admission.admits_global`.
    pub fn admits_global(&self) -> Result<bool, ModelError> {
        self.admits_project(Some(&cps(GLOBAL)))
    }

    /// The [`pool::Admission`] the native search takes, for the rows of
    /// the store `conn` reads: `admits_project` of every project a
    /// ``homes`` row or the projects table names and of ``global``, and
    /// `admits_row` of a global record with every scope list a memory or
    /// a live digest row holds (as `gen_store_path._admission_data`
    /// tabulates it). A home or an id that is not text (or holds a lone
    /// surrogate) is left out, as the pool reads text homes only.
    pub fn pool_admission(&self, conn: &Connection) -> Result<pool::Admission, ModelError> {
        let mut out = pool::Admission::default();
        let mut keys: Vec<String> = vec![GLOBAL.to_owned()];
        let mut statement = conn
            .prepare("SELECT DISTINCT project FROM homes")
            .map_err(sql)?;
        let mut rows = statement.query([]).map_err(sql)?;
        while let Some(row) = rows.next().map_err(sql)? {
            if let ValueRef::Text(b) = row.get_ref(0).map_err(sql)? {
                if let Ok(s) = std::str::from_utf8(b) {
                    keys.push(s.to_owned());
                }
            }
        }
        if let Ok(all) = self.model.all_projects() {
            keys.extend(all.iter().filter_map(|p| unicode::to_string(&p.id)));
        }
        for key in keys {
            let admits = self.admits_project(Some(&cps(&key)))?;
            out.projects.insert(key, admits);
        }
        let mut lists: Vec<String> = Vec::new();
        for select in [
            "SELECT DISTINCT scopes_json FROM memories",
            "SELECT DISTINCT scopes FROM digest_records WHERE removed = 0",
        ] {
            let Ok(mut statement) = conn.prepare(select) else {
                continue;
            };
            let Ok(mut rows) = statement.query([]) else {
                continue;
            };
            while let Ok(Some(row)) = rows.next() {
                if let Ok(ValueRef::Text(b)) = row.get_ref(0) {
                    if let Ok(s) = std::str::from_utf8(b) {
                        lists.push(s.to_owned());
                    }
                }
            }
        }
        for text in lists {
            let Ok(Some(scopes)) = pool::scope_list(&text) else {
                continue;
            };
            if out.global_rows.contains_key(&scopes) {
                continue;
            }
            let wide: Vec<PyStr> = scopes.iter().map(|s| cps(s)).collect();
            let admits = self.admits_row(&wide, Some(&cps(GLOBAL)))?;
            out.global_rows.insert(scopes, admits);
        }
        Ok(out)
    }
}
