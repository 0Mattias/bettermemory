//! Validated records: the reference's pydantic models (`models.Memory`,
//! `TombstonedMemory`, `Episode`, `MemoryLink`, `origin_model.Origin`,
//! `identity.Actor`) as Rust values, read from a log payload
//! ([`Memory::from_payload`] and kin, `<Model>.model_validate`) or from a
//! row ([`rows`], `store_rows._row_to_memory` and kin), and written back to
//! the payload text (`log.canonical_payload` of `models.record_payload` or
//! `model_dump(mode="json")`) and to the column values `store_rows` writes.
//!
//! The error contract. A model's refusal is [`RecordError::Validation`]:
//! the ValidationError's title (the model's class name) and its errors in
//! pydantic's order, each a location path and pydantic's error `type`
//! (`string_type`, `missing`, `value_error` ...). Never its message: the
//! English prose is pydantic's and is not reproduced. Any other exception
//! the reference raises on the way (a row whose `created` is not text, a
//! JSON column that does not parse, `int()` of a word) is
//! [`RecordError::Raised`] with the exception's class name, exactly as
//! `type(exc).__name__` spells it (`AttributeError`, `JSONDecodeError`,
//! `UnicodeDecodeError` ...). `tests/contract/gen_models.py` pins both.
//!
//! Inputs are what the reference's models meet in Python mode, lax: a
//! [`PyValue`] or bytes ([`lax::Arg`]). Unknown fields are ignored, as
//! pydantic's default `extra="ignore"` does.

pub mod lax;
pub mod rows;
pub mod stored;
pub mod summaries;

use crate::datetime::DateTime;
use crate::json::{self, DumpOptions};
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::search::Record;
use crate::unicode;
use lax::{Arg, Kind};

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/// One element of an error's location: a field or dict key, or a list
/// index.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LocItem {
    Key(PyStr),
    Index(usize),
}

/// One of a ValidationError's errors: where, and pydantic's `type`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LineError {
    pub loc: Vec<LocItem>,
    pub kind: &'static str,
}

/// Why a record could not be built.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RecordError {
    /// pydantic's ValidationError: the model's title and the errors in
    /// order (the error count is their number).
    Validation {
        title: &'static str,
        errors: Vec<LineError>,
    },
    /// Another exception, by class name.
    Raised(&'static str),
}

impl RecordError {
    /// The exception's class name.
    pub fn class(&self) -> &'static str {
        match self {
            RecordError::Validation { .. } => "ValidationError",
            RecordError::Raised(name) => name,
        }
    }
}

fn key(name: &str) -> LocItem {
    LocItem::Key(unicode::cps(name))
}

fn one(kind: Kind) -> Vec<LineError> {
    vec![LineError {
        loc: Vec::new(),
        kind,
    }]
}

// ---------------------------------------------------------------------------
// The input a model validates
// ---------------------------------------------------------------------------

/// A mapping of field name to input value: the keyword arguments a model
/// is called with, or the dict `model_validate` reads.
#[derive(Clone, Debug, Default)]
pub struct Fields<'a> {
    items: Vec<(PyStr, Arg<'a>)>,
}

impl<'a> Fields<'a> {
    /// The fields of a dict input; None when the input is not a dict.
    pub fn of(value: &'a PyValue) -> Option<Fields<'a>> {
        let PyValue::Dict(items) = value else {
            return None;
        };
        Some(Fields {
            items: items
                .iter()
                .filter_map(|(k, v)| match k {
                    PyValue::Str(s) => Some((s.clone(), Arg::Py(v))),
                    _ => None,
                })
                .collect(),
        })
    }

    /// Add one field (a later one with the same name wins).
    pub fn push(&mut self, name: &str, value: Arg<'a>) {
        self.items.push((unicode::cps(name), value));
    }

    fn get(&self, name: &str) -> Option<Arg<'a>> {
        let name = unicode::cps(name);
        self.items
            .iter()
            .rev()
            .find(|(k, _)| *k == name)
            .map(|(_, v)| *v)
    }
}

/// A model's validation in progress: errors collected in field order.
struct Check<'f, 'a> {
    fields: &'f Fields<'a>,
    errors: Vec<LineError>,
}

impl<'a> Check<'_, 'a> {
    /// A field's value through `validate`; a missing field is `missing`
    /// when there is no default, else the default.
    fn field<T>(
        &mut self,
        name: &str,
        default: Option<fn() -> T>,
        validate: impl FnOnce(Arg<'a>) -> Result<T, Vec<LineError>>,
    ) -> Option<T> {
        match self.fields.get(name) {
            None => match default {
                Some(make) => Some(make()),
                None => {
                    self.errors.push(LineError {
                        loc: vec![key(name)],
                        kind: "missing",
                    });
                    None
                }
            },
            Some(arg) => match validate(arg) {
                Ok(v) => Some(v),
                Err(errors) => {
                    for mut e in errors {
                        e.loc.insert(0, key(name));
                        self.errors.push(e);
                    }
                    None
                }
            },
        }
    }

    fn finish<T>(
        self,
        title: &'static str,
        build: impl FnOnce() -> Option<T>,
    ) -> Result<T, RecordError> {
        if self.errors.is_empty() {
            if let Some(v) = build() {
                return Ok(v);
            }
        }
        Err(RecordError::Validation {
            title,
            errors: self.errors,
        })
    }
}

fn simple<T>(r: Result<T, Kind>) -> Result<T, Vec<LineError>> {
    r.map_err(one)
}

fn s(arg: Arg<'_>) -> Result<PyStr, Vec<LineError>> {
    simple(lax::string(arg))
}

fn opt_s(arg: Arg<'_>) -> Result<Option<PyStr>, Vec<LineError>> {
    simple(lax::opt_string(arg))
}

fn dt(arg: Arg<'_>) -> Result<DateTime, Vec<LineError>> {
    simple(lax::datetime(arg))
}

fn opt_dt(arg: Arg<'_>) -> Result<Option<DateTime>, Vec<LineError>> {
    simple(lax::opt_datetime(arg))
}

/// `list[str]`.
fn str_list(arg: Arg<'_>) -> Result<Vec<PyStr>, Vec<LineError>> {
    // pydantic-core's lax list takes a tuple too.
    let Arg::Py(PyValue::List(items) | PyValue::Tuple(items)) = arg else {
        return Err(one("list_type"));
    };
    let mut out = Vec::with_capacity(items.len());
    let mut errors = Vec::new();
    for (i, item) in items.iter().enumerate() {
        match lax::string(Arg::Py(item)) {
            Ok(v) => out.push(v),
            Err(kind) => errors.push(LineError {
                loc: vec![LocItem::Index(i)],
                kind,
            }),
        }
    }
    if errors.is_empty() {
        Ok(out)
    } else {
        Err(errors)
    }
}

/// A dict key as pydantic-core rebuilds it: the key's UTF-8 with lone
/// surrogates encoded as themselves (`surrogatepass`), read back lossily,
/// so each surrogate becomes three U+FFFD. An error's location and the
/// JSON-mode dump carry the rebuilt key; the validated record keeps the
/// key as given (so `validate_path_targets` matches it exactly).
pub fn lossy_key(key: &[u32]) -> PyStr {
    if !key.iter().any(|&c| (0xD800..0xE000).contains(&c)) {
        return key.to_vec();
    }
    let mut bytes = Vec::with_capacity(key.len() * 3);
    for &c in key {
        if c < 0x80 {
            bytes.push(c as u8);
        } else if c < 0x800 {
            bytes.extend_from_slice(&[0xC0 | (c >> 6) as u8, 0x80 | (c & 0x3F) as u8]);
        } else if c < 0x10000 {
            bytes.extend_from_slice(&[
                0xE0 | (c >> 12) as u8,
                0x80 | ((c >> 6) & 0x3F) as u8,
                0x80 | (c & 0x3F) as u8,
            ]);
        } else {
            bytes.extend_from_slice(&[
                0xF0 | (c >> 18) as u8,
                0x80 | ((c >> 12) & 0x3F) as u8,
                0x80 | ((c >> 6) & 0x3F) as u8,
                0x80 | (c & 0x3F) as u8,
            ]);
        }
    }
    String::from_utf8_lossy(&bytes)
        .chars()
        .map(u32::from)
        .collect()
}

/// A dict of `str` to `T` through `value`.
fn str_dict<T>(
    arg: Arg<'_>,
    value: impl Fn(Arg<'_>) -> Result<T, Kind>,
) -> Result<Vec<(PyStr, T)>, Vec<LineError>> {
    let Arg::Py(PyValue::Dict(items)) = arg else {
        return Err(one("dict_type"));
    };
    let mut out: Vec<(PyStr, T)> = Vec::with_capacity(items.len());
    // Key to its position in `out`: last wins in place, in linear time.
    let mut at: std::collections::HashMap<PyStr, usize> = std::collections::HashMap::new();
    let mut errors = Vec::new();
    for (k, v) in items {
        let PyValue::Str(k) = k else {
            errors.push(LineError {
                loc: Vec::new(),
                kind: "dict_type",
            });
            continue;
        };
        match value(Arg::Py(v)) {
            Ok(v) => match at.get(k) {
                Some(&i) => out[i].1 = v,
                None => {
                    at.insert(k.clone(), out.len());
                    out.push((k.clone(), v));
                }
            },
            Err(kind) => errors.push(LineError {
                loc: vec![LocItem::Key(lossy_key(k))],
                kind,
            }),
        }
    }
    if errors.is_empty() {
        Ok(out)
    } else {
        Err(errors)
    }
}

/// A nested model field: a dict validated by `validate`, its errors
/// under the field.
fn nested<T>(
    arg: Arg<'_>,
    validate: impl FnOnce(&Fields<'_>) -> Result<T, RecordError>,
) -> Result<T, Vec<LineError>> {
    let Arg::Py(value) = arg else {
        return Err(one("model_type"));
    };
    let Some(fields) = Fields::of(value) else {
        return Err(one("model_type"));
    };
    match validate(&fields) {
        Ok(v) => Ok(v),
        Err(RecordError::Validation { errors, .. }) => Err(errors),
        Err(RecordError::Raised(_)) => Err(one("value_error")),
    }
}

fn opt_nested<T>(
    arg: Arg<'_>,
    validate: impl FnOnce(&Fields<'_>) -> Result<T, RecordError>,
) -> Result<Option<T>, Vec<LineError>> {
    match arg {
        Arg::Py(PyValue::None) => Ok(None),
        other => nested(other, validate).map(Some),
    }
}

/// A model validated from a whole input value: `model_type` at the root
/// when it is not a dict.
fn from_value<T>(
    value: &PyValue,
    title: &'static str,
    validate: impl FnOnce(&Fields<'_>) -> Result<T, RecordError>,
) -> Result<T, RecordError> {
    match Fields::of(value) {
        Some(fields) => validate(&fields),
        None => Err(RecordError::Validation {
            title,
            errors: one("model_type"),
        }),
    }
}

fn value_error() -> Vec<LineError> {
    one("value_error")
}

// ---------------------------------------------------------------------------
// The validators `models` adds
// ---------------------------------------------------------------------------

/// `models._ULID_RE`.
pub fn is_valid_ulid(text: &[u32]) -> bool {
    text.len() == 26
        && text.iter().all(|&c| {
            matches!(c, 0x30..=0x39 | 0x41..=0x48 | 0x4A | 0x4B | 0x4D | 0x4E | 0x50..=0x54 | 0x56..=0x5A)
        })
}

/// `models._SCOPE_RE`: `^[a-z0-9]+(?:[-:][a-z0-9]+)*\Z`.
pub fn is_valid_scope(text: &[u32]) -> bool {
    let word = |c: u32| matches!(c, 0x61..=0x7A | 0x30..=0x39);
    let mut previous_sep = true;
    for &c in text {
        if word(c) {
            previous_sep = false;
        } else if c == u32::from(b'-') || c == u32::from(b':') {
            if previous_sep {
                return false;
            }
            previous_sep = true;
        } else {
            return false;
        }
    }
    !previous_sep
}

/// `models._validate_scopes_list`.
fn scopes_list(v: Vec<PyStr>, allow_empty: bool) -> Result<Vec<PyStr>, Vec<LineError>> {
    if (v.is_empty() && !allow_empty) || v.len() > 64 || !v.iter().all(|s| is_valid_scope(s)) {
        return Err(value_error());
    }
    Ok(v)
}

/// `origin.is_full_commit_sha`.
pub fn is_full_commit_sha(text: &[u32]) -> bool {
    (text.len() == 40 || text.len() == 64)
        && text.iter().all(|&c| matches!(c, 0x30..=0x39 | 0x61..=0x66))
}

/// `str(v).strip().lower()` of a `str`, then the hash check: the anchor
/// `models._validate_verified_head` and `store_rows._anchor_or_none` keep.
pub fn commit_anchor(text: &[u32]) -> Option<PyStr> {
    let start = text.iter().position(|&c| !unicode::is_space(c))?;
    let end = text.iter().rposition(|&c| !unicode::is_space(c))? + 1;
    let lowered = unicode::lower(&text[start..end]);
    is_full_commit_sha(&lowered).then_some(lowered)
}

fn capped_list(arg: Arg<'_>) -> Result<Vec<PyStr>, Vec<LineError>> {
    let v = str_list(arg)?;
    if v.len() > 64 {
        return Err(value_error());
    }
    Ok(v)
}

type Targets = Vec<(PyStr, Option<PyStr>)>;

fn path_targets(arg: Arg<'_>) -> Result<Option<Targets>, Vec<LineError>> {
    if let Arg::Py(PyValue::None) = arg {
        return Ok(None);
    }
    let v = str_dict(arg, lax::opt_string)?;
    if v.len() > 64 {
        return Err(value_error());
    }
    Ok(Some(v))
}

fn verified_head(arg: Arg<'_>) -> Result<Option<PyStr>, Vec<LineError>> {
    match opt_s(arg)? {
        None => Ok(None),
        Some(v) => commit_anchor(&v).map(Some).ok_or_else(value_error),
    }
}

fn ulid(arg: Arg<'_>) -> Result<PyStr, Vec<LineError>> {
    let v = s(arg)?;
    if is_valid_ulid(&v) {
        Ok(v)
    } else {
        Err(value_error())
    }
}

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

macro_rules! str_enum {
    ($(#[$doc:meta])* $name:ident { $($variant:ident = $value:literal),* $(,)? }) => {
        $(#[$doc])*
        #[derive(Clone, Copy, Debug, PartialEq, Eq)]
        pub enum $name { $($variant),* }

        impl $name {
            pub const MEMBERS: &'static [(&'static str, $name)] = &[$(($value, $name::$variant)),*];

            /// The member's value.
            pub fn value(self) -> &'static str {
                match self { $($name::$variant => $value),* }
            }

            fn validate(arg: Arg<'_>) -> Result<$name, Vec<LineError>> {
                simple(lax::enum_member(arg, Self::MEMBERS))
            }
        }
    };
}

str_enum!(
    /// `models.Confidence`.
    Confidence { High = "high", Medium = "medium", Low = "low" }
);
str_enum!(
    /// `models.Source`.
    Source { Explicit = "explicit-statement", Inferred = "inferred", Correction = "user-correction" }
);
str_enum!(
    /// `models.Category`.
    Category { Fact = "fact", UserInference = "user-inference", Ambient = "ambient" }
);
str_enum!(
    /// `models.LinkType`.
    LinkType { Supersedes = "supersedes", Contradicts = "contradicts", Extends = "extends", DependsOn = "depends_on" }
);

// ---------------------------------------------------------------------------
// Origin, Actor, MemoryLink
// ---------------------------------------------------------------------------

/// `origin_model.Origin` as a stored record carries it (the private
/// capture state is never stored).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Origin {
    pub cwd: Option<PyStr>,
    pub repo: Option<PyStr>,
    pub branch: Option<PyStr>,
    pub worktree_root: Option<PyStr>,
    pub source: Option<PyStr>,
}

impl Origin {
    pub const TITLE: &'static str = "Origin";

    fn validate(f: &Fields<'_>) -> Result<Origin, RecordError> {
        let mut c = Check {
            fields: f,
            errors: Vec::new(),
        };
        let cwd = c.field("cwd", Some(|| None), opt_s);
        let repo = c.field("repo", Some(|| None), opt_s);
        let branch = c.field("branch", Some(|| None), opt_s);
        let worktree_root = c.field("worktree_root", Some(|| None), opt_s);
        let source = c.field("source", Some(|| None), opt_s);
        c.finish(Self::TITLE, || {
            Some(Origin {
                cwd: cwd?,
                repo: repo?,
                branch: branch?,
                worktree_root: worktree_root?,
                source: source?,
            })
        })
    }

    /// `Origin.model_validate(value)`.
    pub fn from_value(value: &PyValue) -> Result<Origin, RecordError> {
        from_value(value, Self::TITLE, Self::validate)
    }

    fn entries(&self) -> [(&'static str, &Option<PyStr>); 5] {
        [
            ("cwd", &self.cwd),
            ("repo", &self.repo),
            ("branch", &self.branch),
            ("worktree_root", &self.worktree_root),
            ("source", &self.source),
        ]
    }

    /// `model_dump(mode="json")`, or with `exclude_none`.
    pub fn dump(&self, exclude_none: bool) -> PyValue {
        PyValue::Dict(
            self.entries()
                .into_iter()
                .filter(|(_, v)| !(exclude_none && v.is_none()))
                .map(|(k, v)| (PyValue::str(k), opt_str_value(v)))
                .collect(),
        )
    }
}

/// `identity.Actor` as stored.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Actor {
    pub client: Option<PyStr>,
    pub client_version: Option<PyStr>,
    pub model: Option<PyStr>,
    pub principal: Option<PyStr>,
    pub session: Option<PyStr>,
    pub sources: Vec<(PyStr, PyStr)>,
}

impl Actor {
    pub const TITLE: &'static str = "Actor";

    fn validate(f: &Fields<'_>) -> Result<Actor, RecordError> {
        let mut c = Check {
            fields: f,
            errors: Vec::new(),
        };
        let client = c.field("client", Some(|| None), opt_s);
        let client_version = c.field("client_version", Some(|| None), opt_s);
        let model = c.field("model", Some(|| None), opt_s);
        let principal = c.field("principal", Some(|| None), opt_s);
        let session = c.field("session", Some(|| None), opt_s);
        let sources = c.field("sources", Some(Vec::new), |a| str_dict(a, lax::string));
        c.finish(Self::TITLE, || {
            Some(Actor {
                client: client?,
                client_version: client_version?,
                model: model?,
                principal: principal?,
                session: session?,
                sources: sources?,
            })
        })
    }

    /// `Actor.model_validate(value)`.
    pub fn from_value(value: &PyValue) -> Result<Actor, RecordError> {
        from_value(value, Self::TITLE, Self::validate)
    }

    fn sources_value(&self) -> PyValue {
        lossy_dict(
            self.sources
                .iter()
                .map(|(k, v)| (k, PyValue::Str(v.clone()))),
        )
    }

    /// `model_dump(mode="json")`.
    pub fn dump(&self) -> PyValue {
        let mut items: Vec<(PyValue, PyValue)> = self
            .optional()
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), opt_str_value(v)))
            .collect();
        items.push((PyValue::str("sources"), self.sources_value()));
        PyValue::Dict(items)
    }

    fn optional(&self) -> [(&'static str, &Option<PyStr>); 5] {
        [
            ("client", &self.client),
            ("client_version", &self.client_version),
            ("model", &self.model),
            ("principal", &self.principal),
            ("session", &self.session),
        ]
    }

    /// `Actor.to_record`: set fields only, `sources` only when non-empty.
    pub fn to_record(&self) -> PyValue {
        let mut items: Vec<(PyValue, PyValue)> = self
            .optional()
            .into_iter()
            .filter_map(|(k, v)| {
                v.as_ref()
                    .map(|v| (PyValue::str(k), PyValue::Str(v.clone())))
            })
            .collect();
        if !self.sources.is_empty() {
            items.push((PyValue::str("sources"), self.sources_value()));
        }
        PyValue::Dict(items)
    }
}

/// `models.MemoryLink`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MemoryLink {
    pub link_type: LinkType,
    pub target_id: PyStr,
    pub note: Option<PyStr>,
}

impl MemoryLink {
    pub const TITLE: &'static str = "MemoryLink";

    fn validate(f: &Fields<'_>) -> Result<MemoryLink, RecordError> {
        let mut c = Check {
            fields: f,
            errors: Vec::new(),
        };
        let link_type = c.field("type", None, LinkType::validate);
        let target_id = c.field("target_id", None, ulid);
        let note = c.field("note", Some(|| None), opt_s);
        c.finish(Self::TITLE, || {
            Some(MemoryLink {
                link_type: link_type?,
                target_id: target_id?,
                note: note?,
            })
        })
    }

    /// `MemoryLink.model_validate(value)`.
    pub fn from_value(value: &PyValue) -> Result<MemoryLink, RecordError> {
        from_value(value, Self::TITLE, Self::validate)
    }

    /// `model_dump(mode="json")`, or with `exclude_none`.
    pub fn dump(&self, exclude_none: bool) -> PyValue {
        let mut items = vec![
            (PyValue::str("type"), PyValue::str(self.link_type.value())),
            (
                PyValue::str("target_id"),
                PyValue::Str(self.target_id.clone()),
            ),
        ];
        if !(exclude_none && self.note.is_none()) {
            items.push((PyValue::str("note"), opt_str_value(&self.note)));
        }
        PyValue::Dict(items)
    }
}

fn links(arg: Arg<'_>, id: Option<&PyStr>) -> Result<Vec<MemoryLink>, Vec<LineError>> {
    // pydantic-core's lax list takes a tuple too.
    let Arg::Py(PyValue::List(items) | PyValue::Tuple(items)) = arg else {
        return Err(one("list_type"));
    };
    let mut out = Vec::with_capacity(items.len());
    let mut errors = Vec::new();
    for (i, item) in items.iter().enumerate() {
        match nested(Arg::Py(item), MemoryLink::validate) {
            Ok(v) => out.push(v),
            Err(es) => errors.extend(es.into_iter().map(|mut e| {
                e.loc.insert(0, LocItem::Index(i));
                e
            })),
        }
    }
    if !errors.is_empty() {
        return Err(errors);
    }
    if let Some(id) = id {
        if out.iter().any(|l| &l.target_id == id) {
            return Err(value_error());
        }
    }
    if out.len() > 64 {
        return Err(value_error());
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Memory and TombstonedMemory
// ---------------------------------------------------------------------------

/// The fields a memory and a tombstone share, as the models define them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RecordFields {
    pub id: PyStr,
    pub created: DateTime,
    pub updated: DateTime,
    pub scopes: Vec<PyStr>,
    pub confidence: Confidence,
    pub source: Source,
    pub body: PyStr,
    pub origin: Option<Origin>,
    pub actor: Option<Actor>,
    pub last_verified_at: Option<DateTime>,
    pub category: Option<Category>,
    pub verified_paths: Vec<PyStr>,
    pub verified_commits: Vec<PyStr>,
    pub verified_versions: Vec<PyStr>,
    pub verified_absent_paths: Vec<PyStr>,
    pub verified_path_targets: Option<Targets>,
    pub verified_absent_path_targets: Option<Targets>,
    pub claims: Vec<PyStr>,
    pub verified_head: Option<PyStr>,
}

/// `models.Memory`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Memory {
    pub fields: RecordFields,
    pub links: Vec<MemoryLink>,
    /// Canonical decimal text: a Python int has no bound.
    pub corroborations: String,
    pub last_corroborated: Option<DateTime>,
}

/// `models.TombstonedMemory`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Tombstone {
    pub fields: RecordFields,
    pub removed: DateTime,
    pub removed_reason: PyStr,
    pub removed_session: Option<PyStr>,
}

fn non_negative(arg: Arg<'_>) -> Result<String, Vec<LineError>> {
    let v = simple(lax::int(arg))?;
    if v.starts_with('-') {
        return Err(one("greater_than_equal"));
    }
    Ok(v)
}

/// The shared fields through `verified_head`, in the models' order; the
/// caller's own fields follow. Returns None where a field failed.
#[allow(clippy::type_complexity)]
fn shared(c: &mut Check<'_, '_>) -> Option<RecordFields> {
    let id = c.field("id", None, ulid);
    let created = c.field("created", None, dt);
    let updated = c.field("updated", None, dt);
    let scopes = c.field("scopes", None, |a| {
        let v = str_list(a)?;
        if v.is_empty() {
            return Err(one("too_short"));
        }
        scopes_list(v, false)
    });
    let confidence = c.field("confidence", None, Confidence::validate);
    let source = c.field("source", None, Source::validate);
    let body = c.field("body", None, s);
    let origin = c.field("origin", Some(|| None), |a| opt_nested(a, Origin::validate));
    let actor = c.field("actor", Some(|| None), |a| opt_nested(a, Actor::validate));
    let last_verified_at = c.field("last_verified_at", Some(|| None), opt_dt);
    let category = c.field("category", Some(|| None), |a| match a {
        Arg::Py(PyValue::None) => Ok(None),
        other => Category::validate(other).map(Some),
    });
    let verified_paths = c.field("verified_paths", Some(Vec::new), capped_list);
    let verified_commits = c.field("verified_commits", Some(Vec::new), capped_list);
    let verified_versions = c.field("verified_versions", Some(Vec::new), capped_list);
    let verified_absent_paths = c.field("verified_absent_paths", Some(Vec::new), capped_list);
    let verified_path_targets = c.field("verified_path_targets", Some(|| None), path_targets);
    let verified_absent_path_targets =
        c.field("verified_absent_path_targets", Some(|| None), path_targets);
    let claims = c.field("claims", Some(Vec::new), capped_list);
    let verified_head = c.field("verified_head", Some(|| None), verified_head);
    Some(RecordFields {
        id: id?,
        created: created?,
        updated: updated?,
        scopes: scopes?,
        confidence: confidence?,
        source: source?,
        body: body?,
        origin: origin?,
        actor: actor?,
        last_verified_at: last_verified_at?,
        category: category?,
        verified_paths: verified_paths?,
        verified_commits: verified_commits?,
        verified_versions: verified_versions?,
        verified_absent_paths: verified_absent_paths?,
        verified_path_targets: verified_path_targets?,
        verified_absent_path_targets: verified_absent_path_targets?,
        claims: claims?,
        verified_head: verified_head?,
    })
}

/// The id a cross-field validator sees in `info.data`: present only when
/// `id` itself validated.
fn validated_id(f: &Fields<'_>) -> Option<PyStr> {
    f.get("id").and_then(|a| ulid(a).ok())
}

impl Memory {
    pub const TITLE: &'static str = "Memory";

    /// `Memory(**fields)`.
    pub fn validate(f: &Fields<'_>) -> Result<Memory, RecordError> {
        let mut c = Check {
            fields: f,
            errors: Vec::new(),
        };
        let fields = shared(&mut c);
        let id = validated_id(f);
        let links = c.field("links", Some(Vec::new), |a| links(a, id.as_ref()));
        let corroborations = c.field("corroborations", Some(|| "0".to_owned()), non_negative);
        let last_corroborated = c.field("last_corroborated", Some(|| None), opt_dt);
        c.finish(Self::TITLE, || {
            Some(Memory {
                fields: fields?,
                links: links?,
                corroborations: corroborations?,
                last_corroborated: last_corroborated?,
            })
        })
    }

    /// `Memory.model_validate(value)`: a `memory_put` payload's record.
    pub fn from_payload(value: &PyValue) -> Result<Memory, RecordError> {
        from_value(value, Self::TITLE, Self::validate)
    }

    /// `model_dump(mode="json")`.
    pub fn dump(&self) -> PyValue {
        let mut items = self.fields.dump_items();
        items.push((
            PyValue::str("links"),
            PyValue::List(self.links.iter().map(|l| l.dump(false)).collect()),
        ));
        items.push((
            PyValue::str("corroborations"),
            PyValue::Int(self.corroborations.clone()),
        ));
        items.push((
            PyValue::str("last_corroborated"),
            opt_dt_value(&self.last_corroborated),
        ));
        PyValue::Dict(items)
    }

    /// `models.record_payload`: the dump with unset target maps left out;
    /// ValueError where `validate_path_targets` refuses the record.
    pub fn record_payload(&self) -> Result<PyValue, PyError> {
        self.fields.check_path_targets()?;
        Ok(drop_unset_targets(self.dump()))
    }

    /// `log.canonical_payload(record_payload(memory))`.
    pub fn payload_text(&self) -> Result<String, PyError> {
        json::canonical_payload(&self.record_payload()?)
    }

    /// The columns `store_rows._write_memory_row` writes from the record,
    /// in [`MEMORY_ROW_COLUMNS`] order (the index text, the filename, the
    /// provenance and the log MAC come from elsewhere).
    pub fn row(&self) -> Result<Vec<PyValue>, PyError> {
        let mut row = self.fields.columns()?;
        row.push(PyValue::Str(scopes_text(&self.fields.scopes)));
        row.push(PyValue::Str(links_json(&self.links)?));
        row.push(PyValue::Int(self.corroborations.clone()));
        row.push(opt_iso(&self.last_corroborated));
        Ok(row)
    }

    /// The ranker's record (`search::Record`); None when the id or a
    /// scope holds a lone surrogate (a validated one cannot).
    pub fn to_search_record(&self) -> Option<Record> {
        Some(Record {
            id: unicode::to_string(&self.fields.id)?,
            created: self.fields.created,
            updated: self.fields.updated,
            scopes: self
                .fields
                .scopes
                .iter()
                .map(|s| unicode::to_string(s))
                .collect::<Option<Vec<String>>>()?,
            body: self.fields.body.clone(),
        })
    }
}

impl Tombstone {
    pub const TITLE: &'static str = "TombstonedMemory";

    /// `TombstonedMemory(**fields)`.
    pub fn validate(f: &Fields<'_>) -> Result<Tombstone, RecordError> {
        let mut c = Check {
            fields: f,
            errors: Vec::new(),
        };
        let fields = shared(&mut c);
        let removed = c.field("removed", None, dt);
        let removed_reason = c.field("removed_reason", None, s);
        let removed_session = c.field("removed_session", Some(|| None), opt_s);
        c.finish(Self::TITLE, || {
            Some(Tombstone {
                fields: fields?,
                removed: removed?,
                removed_reason: removed_reason?,
                removed_session: removed_session?,
            })
        })
    }

    /// `TombstonedMemory.model_validate(value)`.
    pub fn from_payload(value: &PyValue) -> Result<Tombstone, RecordError> {
        from_value(value, Self::TITLE, Self::validate)
    }

    /// `model_dump(mode="json")`.
    pub fn dump(&self) -> PyValue {
        let mut items = self.fields.dump_items();
        items.push((PyValue::str("removed"), dt_value(&self.removed)));
        items.push((
            PyValue::str("removed_reason"),
            PyValue::Str(self.removed_reason.clone()),
        ));
        items.push((
            PyValue::str("removed_session"),
            opt_str_value(&self.removed_session),
        ));
        PyValue::Dict(items)
    }

    /// `models.record_payload`.
    pub fn record_payload(&self) -> Result<PyValue, PyError> {
        self.fields.check_path_targets()?;
        Ok(drop_unset_targets(self.dump()))
    }

    /// `log.canonical_payload(record_payload(tombstone))`.
    pub fn payload_text(&self) -> Result<String, PyError> {
        json::canonical_payload(&self.record_payload()?)
    }

    /// The columns `_write_tombstone_row` writes from the record, in
    /// [`TOMBSTONE_ROW_COLUMNS`] order.
    pub fn row(&self) -> Result<Vec<PyValue>, PyError> {
        let mut row = self.fields.columns()?;
        row.push(PyValue::Str(unicode::cps(&self.removed.isoformat())));
        row.push(PyValue::Str(self.removed_reason.clone()));
        row.push(opt_str_value(&self.removed_session));
        Ok(row)
    }
}

/// `store_rows._record_columns`' names, in the order [`Memory::row`] and
/// [`Tombstone::row`] give them first.
pub const RECORD_COLUMNS: [&str; 21] = [
    "id",
    "created",
    "updated",
    "last_verified_at",
    "confidence",
    "category",
    "body",
    "scopes_json",
    "origin_repo",
    "origin_worktree",
    "verified_head",
    "actor_client",
    "actor_model",
    "source",
    "origin_json",
    "actor_json",
    "verified_paths_json",
    "verified_commits_json",
    "verified_versions_json",
    "verified_absent_paths_json",
    "claims_json",
];

/// [`Memory::row`]'s columns after [`RECORD_COLUMNS`].
pub const MEMORY_ROW_COLUMNS: [&str; 4] = [
    "scopes_text",
    "links_json",
    "corroborations",
    "last_corroborated",
];

/// [`Tombstone::row`]'s columns after [`RECORD_COLUMNS`].
pub const TOMBSTONE_ROW_COLUMNS: [&str; 3] = ["removed", "removed_reason", "removed_session"];

/// [`Episode::row`]'s columns.
pub const EPISODE_ROW_COLUMNS: [&str; 9] = [
    "id",
    "session_id",
    "created",
    "body",
    "scopes_json",
    "takeaway",
    "origin_json",
    "is_floor",
    "swarm_id",
];

fn drop_unset_targets(dump: PyValue) -> PyValue {
    let PyValue::Dict(items) = &dump else {
        return dump;
    };
    PyValue::Dict(
        items
            .iter()
            .filter(|(k, v)| {
                let target = *k == PyValue::str("verified_path_targets")
                    || *k == PyValue::str("verified_absent_path_targets");
                !(target && *v == PyValue::None)
            })
            .cloned()
            .collect(),
    )
}

fn opt_str_value(v: &Option<PyStr>) -> PyValue {
    v.as_ref()
        .map_or(PyValue::None, |s| PyValue::Str(s.clone()))
}

fn dt_value(dt: &DateTime) -> PyValue {
    PyValue::str(&dt.pydantic_json())
}

fn opt_dt_value(v: &Option<DateTime>) -> PyValue {
    v.as_ref().map_or(PyValue::None, dt_value)
}

fn opt_iso(v: &Option<DateTime>) -> PyValue {
    v.as_ref()
        .map_or(PyValue::None, |d| PyValue::str(&d.isoformat()))
}

fn str_list_value(v: &[PyStr]) -> PyValue {
    PyValue::List(v.iter().map(|s| PyValue::Str(s.clone())).collect())
}

fn targets_value(v: &Option<Targets>) -> PyValue {
    match v {
        None => PyValue::None,
        Some(items) => lossy_dict(items.iter().map(|(k, v)| (k, opt_str_value(v)))),
    }
}

/// A dict in `model_dump(mode="json")`: each key rebuilt ([`lossy_key`]);
/// keys that meet after the rebuild collapse as a Python dict built in
/// order does, the first position kept and the last value winning.
fn lossy_dict<'a>(items: impl Iterator<Item = (&'a PyStr, PyValue)>) -> PyValue {
    let mut out: Vec<(PyValue, PyValue)> = Vec::new();
    let mut at: std::collections::HashMap<PyStr, usize> = std::collections::HashMap::new();
    for (k, v) in items {
        let k = lossy_key(k);
        match at.get(&k) {
            Some(&i) => out[i].1 = v,
            None => {
                at.insert(k.clone(), out.len());
                out.push((PyValue::Str(k), v));
            }
        }
    }
    PyValue::Dict(out)
}

/// `store_rows._json_text`: `json.dumps(value, sort_keys=True,
/// ensure_ascii=False)`.
pub fn json_text(value: &PyValue) -> Result<PyStr, PyError> {
    json::dumps(
        value,
        DumpOptions {
            sort_keys: true,
            ensure_ascii: false,
            item_separator: ", ",
            key_separator: ": ",
            indent: None,
            default_str: false,
        },
    )
}

/// `_json_or_none`: the text of a non-empty dict, else None.
fn json_or_none(value: &PyValue) -> Result<PyValue, PyError> {
    match value {
        PyValue::Dict(items) if items.is_empty() => Ok(PyValue::None),
        other => Ok(PyValue::Str(json_text(other)?)),
    }
}

/// `store_rows._origin_json`.
pub fn origin_json(origin: &Option<Origin>) -> Result<PyValue, PyError> {
    match origin {
        None => Ok(PyValue::None),
        Some(o) => json_or_none(&o.dump(true)),
    }
}

/// `store_rows._actor_json`.
pub fn actor_json(actor: &Option<Actor>) -> Result<PyValue, PyError> {
    match actor {
        None => Ok(PyValue::None),
        Some(a) => json_or_none(&a.to_record()),
    }
}

/// `store_rows._links_json`.
pub fn links_json(links: &[MemoryLink]) -> Result<PyStr, PyError> {
    json_text(&PyValue::List(links.iter().map(|l| l.dump(true)).collect()))
}

/// `store_rows._scopes_text`.
pub fn scopes_text(scopes: &[PyStr]) -> PyStr {
    let mut out = vec![u32::from(b' ')];
    for (i, scope) in scopes.iter().enumerate() {
        if i > 0 {
            out.push(u32::from(b' '));
        }
        out.extend_from_slice(scope);
    }
    if !scopes.is_empty() {
        out.push(u32::from(b' '));
    }
    out
}

impl RecordFields {
    fn dump_items(&self) -> Vec<(PyValue, PyValue)> {
        vec![
            (PyValue::str("id"), PyValue::Str(self.id.clone())),
            (PyValue::str("created"), dt_value(&self.created)),
            (PyValue::str("updated"), dt_value(&self.updated)),
            (PyValue::str("scopes"), str_list_value(&self.scopes)),
            (
                PyValue::str("confidence"),
                PyValue::str(self.confidence.value()),
            ),
            (PyValue::str("source"), PyValue::str(self.source.value())),
            (PyValue::str("body"), PyValue::Str(self.body.clone())),
            (
                PyValue::str("origin"),
                self.origin
                    .as_ref()
                    .map_or(PyValue::None, |o| o.dump(false)),
            ),
            (
                PyValue::str("actor"),
                self.actor.as_ref().map_or(PyValue::None, Actor::dump),
            ),
            (
                PyValue::str("last_verified_at"),
                opt_dt_value(&self.last_verified_at),
            ),
            (
                PyValue::str("category"),
                self.category
                    .map_or(PyValue::None, |c| PyValue::str(c.value())),
            ),
            (
                PyValue::str("verified_paths"),
                str_list_value(&self.verified_paths),
            ),
            (
                PyValue::str("verified_commits"),
                str_list_value(&self.verified_commits),
            ),
            (
                PyValue::str("verified_versions"),
                str_list_value(&self.verified_versions),
            ),
            (
                PyValue::str("verified_absent_paths"),
                str_list_value(&self.verified_absent_paths),
            ),
            (
                PyValue::str("verified_path_targets"),
                targets_value(&self.verified_path_targets),
            ),
            (
                PyValue::str("verified_absent_path_targets"),
                targets_value(&self.verified_absent_path_targets),
            ),
            (PyValue::str("claims"), str_list_value(&self.claims)),
            (
                PyValue::str("verified_head"),
                opt_str_value(&self.verified_head),
            ),
        ]
    }

    /// `models.validate_path_targets`: each set target map covers its
    /// legacy list; ValueError otherwise.
    pub fn check_path_targets(&self) -> Result<(), PyError> {
        for (targets, inputs) in [
            (&self.verified_path_targets, &self.verified_paths),
            (
                &self.verified_absent_path_targets,
                &self.verified_absent_paths,
            ),
        ] {
            if let Some(targets) = targets {
                if targets.len() > 64 || !inputs.iter().all(|i| targets.iter().any(|(k, _)| k == i))
                {
                    return Err(PyError::ValueError);
                }
            }
        }
        Ok(())
    }

    /// `_record_columns`, in [`RECORD_COLUMNS`] order.
    fn columns(&self) -> Result<Vec<PyValue>, PyError> {
        self.check_path_targets()?;
        let list = |v: &[PyStr]| json_text(&str_list_value(v)).map(PyValue::Str);
        Ok(vec![
            PyValue::Str(self.id.clone()),
            PyValue::str(&self.created.isoformat()),
            PyValue::str(&self.updated.isoformat()),
            opt_iso(&self.last_verified_at),
            PyValue::str(self.confidence.value()),
            self.category
                .map_or(PyValue::None, |c| PyValue::str(c.value())),
            PyValue::Str(self.body.clone()),
            list(&self.scopes)?,
            self.origin
                .as_ref()
                .map_or(PyValue::None, |o| opt_str_value(&o.repo)),
            self.origin
                .as_ref()
                .map_or(PyValue::None, |o| opt_str_value(&o.worktree_root)),
            opt_str_value(&self.verified_head),
            self.actor
                .as_ref()
                .map_or(PyValue::None, |a| opt_str_value(&a.client)),
            self.actor
                .as_ref()
                .map_or(PyValue::None, |a| opt_str_value(&a.model)),
            PyValue::str(self.source.value()),
            origin_json(&self.origin)?,
            actor_json(&self.actor)?,
            list(&self.verified_paths)?,
            list(&self.verified_commits)?,
            list(&self.verified_versions)?,
            list(&self.verified_absent_paths)?,
            list(&self.claims)?,
        ])
    }
}

// ---------------------------------------------------------------------------
// Episode
// ---------------------------------------------------------------------------

/// `models.Episode`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Episode {
    pub id: PyStr,
    pub session_id: PyStr,
    pub created: DateTime,
    pub body: PyStr,
    pub scopes: Vec<PyStr>,
    pub takeaway: Option<PyStr>,
    pub origin: Option<Origin>,
    pub is_floor: bool,
    pub swarm_id: Option<PyStr>,
}

fn swarm_id(arg: Arg<'_>) -> Result<Option<PyStr>, Vec<LineError>> {
    let Some(v) = opt_s(arg)? else {
        return Ok(None);
    };
    let token = |c: u32| matches!(c, 0x30..=0x39 | 0x41..=0x5A | 0x61..=0x7A | 0x5F | 0x2D);
    if v.is_empty() || v.len() > 128 || !v.iter().all(|&c| token(c)) {
        return Err(value_error());
    }
    Ok(Some(v))
}

impl Episode {
    pub const TITLE: &'static str = "Episode";

    /// `Episode(**fields)`.
    pub fn validate(f: &Fields<'_>) -> Result<Episode, RecordError> {
        let mut c = Check {
            fields: f,
            errors: Vec::new(),
        };
        let id = c.field("id", None, ulid);
        let session_id = c.field("session_id", None, s);
        let created = c.field("created", None, dt);
        let body = c.field("body", None, s);
        let scopes = c.field("scopes", Some(Vec::new), |a| {
            scopes_list(str_list(a)?, true)
        });
        let takeaway = c.field("takeaway", Some(|| None), opt_s);
        let origin = c.field("origin", Some(|| None), |a| opt_nested(a, Origin::validate));
        let is_floor = c.field("is_floor", Some(|| false), |a| simple(lax::boolean(a)));
        let swarm_id = c.field("swarm_id", Some(|| None), swarm_id);
        c.finish(Self::TITLE, || {
            Some(Episode {
                id: id?,
                session_id: session_id?,
                created: created?,
                body: body?,
                scopes: scopes?,
                takeaway: takeaway?,
                origin: origin?,
                is_floor: is_floor?,
                swarm_id: swarm_id?,
            })
        })
    }

    /// `Episode.model_validate(value)`: an `episode_put` payload's record.
    pub fn from_payload(value: &PyValue) -> Result<Episode, RecordError> {
        from_value(value, Self::TITLE, Self::validate)
    }

    /// `model_dump(mode="json")`, which `store_journal` writes as the
    /// payload's `episode`.
    pub fn dump(&self) -> PyValue {
        PyValue::Dict(vec![
            (PyValue::str("id"), PyValue::Str(self.id.clone())),
            (
                PyValue::str("session_id"),
                PyValue::Str(self.session_id.clone()),
            ),
            (PyValue::str("created"), dt_value(&self.created)),
            (PyValue::str("body"), PyValue::Str(self.body.clone())),
            (PyValue::str("scopes"), str_list_value(&self.scopes)),
            (PyValue::str("takeaway"), opt_str_value(&self.takeaway)),
            (
                PyValue::str("origin"),
                self.origin
                    .as_ref()
                    .map_or(PyValue::None, |o| o.dump(false)),
            ),
            (PyValue::str("is_floor"), PyValue::Bool(self.is_floor)),
            (PyValue::str("swarm_id"), opt_str_value(&self.swarm_id)),
        ])
    }

    /// `log.canonical_payload` of the dump.
    pub fn payload_text(&self) -> Result<String, PyError> {
        json::canonical_payload(&self.dump())
    }

    /// The columns `_write_episode_row` writes, in [`EPISODE_ROW_COLUMNS`]
    /// order (the log MAC aside).
    pub fn row(&self) -> Result<Vec<PyValue>, PyError> {
        Ok(vec![
            PyValue::Str(self.id.clone()),
            PyValue::Str(self.session_id.clone()),
            PyValue::str(&self.created.isoformat()),
            PyValue::Str(self.body.clone()),
            PyValue::Str(json_text(&str_list_value(&self.scopes))?),
            opt_str_value(&self.takeaway),
            origin_json(&self.origin)?,
            PyValue::int(i128::from(self.is_floor)),
            opt_str_value(&self.swarm_id),
        ])
    }
}
