//! Read-only projections of a store's key, origin anchor, and head checkpoint.
mod paths;
pub(crate) use paths::{normalize_spelling, resolve_spelling, ring_file};

use super::{log_row, StoreError, LOG_SELECT};
use crate::fsutil::fsdecode;
use crate::log::ring::{KeyFiles, KeyUnreadable};
use crate::log::{fingerprint, same_mac, Head, Key, LogRow, OriginCheck};
use crate::pyvalue::{cmp_keys, py_int, py_str, type_name, PyStr, PyValue};
use crate::unicode::{cps, upper};
use rusqlite::Connection;
use std::cmp::Ordering;
use std::collections::HashSet;
use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};

pub const KEY_PRESENT: &str = "present";
pub const KEY_UNREADABLE: &str = "unreadable";
pub const KEY_MISSING: &str = "missing";
pub const KEY_FOREIGN: &str = "foreign";
pub const KEY_BOUND_ELSEWHERE: &str = "bound_elsewhere";
pub const KEY_UNCLAIMED: &str = "unclaimed";
pub const KEY_STATES: [&str; 6] = [
    KEY_PRESENT,
    KEY_UNREADABLE,
    KEY_MISSING,
    KEY_FOREIGN,
    KEY_BOUND_ELSEWHERE,
    KEY_UNCLAIMED,
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KeyStatus {
    Present,
    Unreadable,
    Missing,
    Foreign,
    BoundElsewhere,
    Unclaimed,
}
impl KeyStatus {
    pub fn name(self) -> &'static str {
        match self {
            Self::Present => KEY_PRESENT,
            Self::Unreadable => KEY_UNREADABLE,
            Self::Missing => KEY_MISSING,
            Self::Foreign => KEY_FOREIGN,
            Self::BoundElsewhere => KEY_BOUND_ELSEWHERE,
            Self::Unclaimed => KEY_UNCLAIMED,
        }
    }
}

pub const HEAD_ABSENT: &str = "absent";
pub const HEAD_OK: &str = "ok";
pub const HEAD_STALE: &str = "stale";
pub const HEAD_PAST: &str = "tail_deleted";
pub const HEAD_MISMATCH: &str = "mismatch";
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HeadStatus {
    Absent,
    Ok,
    Stale,
    Past,
    Mismatch,
}
impl HeadStatus {
    pub fn name(self) -> &'static str {
        match self {
            Self::Absent => HEAD_ABSENT,
            Self::Ok => HEAD_OK,
            Self::Stale => HEAD_STALE,
            Self::Past => HEAD_PAST,
            Self::Mismatch => HEAD_MISMATCH,
        }
    }
}

#[derive(Debug)]
pub enum StateError {
    Read(StoreError),
    Raised { class: &'static str, message: PyStr },
}
impl StateError {
    fn raised(class: &'static str, message: &str) -> Self {
        Self::Raised {
            class,
            message: cps(message),
        }
    }
}
impl fmt::Display for StateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Read(e) => e.fmt(f),
            Self::Raised { message, .. } => {
                f.write_str(&crate::unicode::to_string(message).unwrap_or_else(|| {
                    crate::unicode::to_string(&crate::pyvalue::repr_str(message))
                        .unwrap_or_default()
                }))
            }
        }
    }
}
impl std::error::Error for StateError {}
impl From<rusqlite::Error> for StateError {
    fn from(e: rusqlite::Error) -> Self {
        Self::Read(StoreError::Sqlite(e))
    }
}
impl From<StoreError> for StateError {
    fn from(e: StoreError) -> Self {
        Self::Read(e)
    }
}

fn words(template: &str, fields: &[&[u32]]) -> PyStr {
    let mut parts = template.split("{}");
    let mut result = cps(parts.next().unwrap_or_default());
    for field in fields {
        result.extend_from_slice(field);
        result.extend(cps(parts
            .next()
            .expect("one field per literal placeholder")));
    }
    debug_assert!(parts.next().is_none());
    result
}

fn item(name: &str, value: PyValue) -> (PyValue, PyValue) {
    (PyValue::str(name), value)
}
fn lexical_path(path: &Path) -> PathBuf {
    // Path supplied to Python has already discarded "." and redundant separators.
    let clean: PathBuf = path
        .components()
        .filter(|part| *part != std::path::Component::CurDir)
        .collect();
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        let raw = path.as_os_str().as_bytes();
        if raw.starts_with(b"//") && !raw.starts_with(b"///") {
            let mut text = std::ffi::OsString::from("/");
            text.push(clean.as_os_str());
            return text.into();
        }
    }
    if clean.as_os_str().is_empty() {
        PathBuf::from(".")
    } else {
        clean
    }
}
fn path_text(path: &Path) -> PyStr {
    fsdecode(lexical_path(path).as_os_str())
}
fn display_seq(value: &PyValue) -> Result<PyStr, StateError> {
    if let PyValue::Int(digits) = value {
        if digits.trim_start_matches('-').len() > 4300 {
            return Err(StateError::raised(
                "ValueError",
                crate::toml::INT_STR_LIMIT_MESSAGE,
            ));
        }
    }
    py_str(value).map_err(|e| StateError::raised(e.name(), e.name()))
}
fn first16(text: &[u32]) -> &[u32] {
    &text[..text.len().min(16)]
}
pub fn sentence(text: &[u32]) -> PyStr {
    match text.split_first() {
        None => Vec::new(),
        Some((first, rest)) => {
            let mut t = upper(std::slice::from_ref(first));
            t.extend_from_slice(rest);
            t
        }
    }
}

#[derive(Clone, Debug)]
pub struct RingFit {
    pub origin: OriginCheck,
    pub head: Option<Head>,
    pub head_status: HeadStatus,
    pub log_end: Option<LogRow>,
}
impl RingFit {
    pub fn carries_origin(&self) -> bool {
        !self.origin.broken()
    }
    pub fn reaches_head(&self) -> bool {
        matches!(
            self.head_status,
            HeadStatus::Absent | HeadStatus::Ok | HeadStatus::Stale
        )
    }
    pub fn fits(&self) -> bool {
        self.carries_origin() && self.reaches_head()
    }
    pub fn reason(&self) -> Result<Option<PyStr>, StateError> {
        if !self.carries_origin() {
            let seq = self
                .origin
                .seq
                .map_or_else(|| cps("None"), |n| cps(&n.to_string()));
            let template = match self.origin.reason {
                Some("missing") => "its log holds no row at seq {}, where the key ring's origin anchor says this host's \
                    history of the store begins",
                Some("mac") => {
                    "its row at seq {} carries another MAC than the key ring's origin anchor names"
                }
                _ => "its row at seq {} carries the origin anchor's MAC and does not verify under the key \
                    the anchor names, which this host holds",
            };
            return Ok(Some(words(template, &[&seq])));
        }
        let Some(h) = &self.head else { return Ok(None) };
        if self.reaches_head() {
            return Ok(None);
        }
        let seq = display_seq(&h.seq)?;
        Ok(Some(if self.head_status == HeadStatus::Past {
            let end = self.log_end.as_ref().map_or(0, |r| r.seq);
            words(
                "its log ends at seq {}, before the key ring's head checkpoint at seq {}",
                &[&cps(&end.to_string()), &seq],
            )
        } else {
            words(
                "its row at seq {} carries another MAC than the key ring's head checkpoint",
                &[&seq],
            )
        }))
    }
    pub fn to_dict(&self) -> Result<PyValue, StateError> {
        let mut head = vec![item("status", PyValue::str(self.head_status.name()))];
        if let Some(h) = &self.head {
            head.push(item("seq", h.seq.clone()))
        }
        let mut out = vec![
            item("origin", self.origin.to_dict()),
            item("head", PyValue::Dict(head)),
            item(
                "log_end",
                self.log_end
                    .as_ref()
                    .map_or(PyValue::None, |r| PyValue::int(i128::from(r.seq))),
            ),
            item("fits", PyValue::Bool(self.fits())),
        ];
        if let Some(r) = self.reason()? {
            out.push(item("reason", PyValue::Str(r)))
        }
        Ok(PyValue::Dict(out))
    }
}

/// Read one complete log row. pysqlite decodes all seven selected columns
/// before Python sees the row, including when only its sequence or MAC is used.
pub(super) fn first_log_row(
    conn: &Connection,
    tail: &str,
    params: &[&dyn rusqlite::ToSql],
) -> Result<Option<LogRow>, StateError> {
    let mut statement = conn.prepare(&format!("{LOG_SELECT} {tail}"))?;
    let mut rows = statement.query(params)?;
    let Some(row) = rows.next()? else {
        return Ok(None);
    };
    match log_row(row) {
        Err(StoreError::NotUtf8 { column, .. }) => {
            let bytes = row
                .get_ref(column)?
                .as_bytes()
                .expect("NotUtf8 identifies a text column");
            Err(StateError::raised(
                "OperationalError",
                &super::sqlite_text_error(column, bytes),
            ))
        }
        result => Ok(Some(result?)),
    }
}

/// Check the tip before inquiring about an older row. Retain the caller's transaction
/// and the reader's column coercions. In particular an oversized positive sequence needs
/// no SQLite bind, whereas a sequence below the SQLite range does.
pub fn head_status(head: Option<&Head>, conn: &Connection) -> Result<HeadStatus, StateError> {
    let Some(h) = head else {
        return Ok(HeadStatus::Absent);
    };
    let Some(last) = first_log_row(conn, "ORDER BY seq DESC LIMIT 1", &[])? else {
        return Ok(HeadStatus::Past);
    };
    if matches!(h.seq, PyValue::Float(f) if f.is_nan()) {
        return Err(StateError::raised(
            "ValueError",
            "cannot convert float NaN to integer",
        ));
    }
    match cmp_keys(&h.seq, &PyValue::int(i128::from(last.seq))) {
        Some(Ordering::Greater) => return Ok(HeadStatus::Past),
        Some(Ordering::Equal) => {
            return Ok(if same_mac(&h.mac, &cps(&last.mac)) {
                HeadStatus::Ok
            } else {
                HeadStatus::Mismatch
            })
        }
        Some(Ordering::Less) => {}
        None => {
            return Err(StateError::raised(
                "TypeError",
                &format!(
                    "'>' not supported between instances of '{}' and 'int'",
                    type_name(&h.seq)
                ),
            ))
        }
    }
    let lower = match &h.seq {
        PyValue::Int(x) => x.parse::<i64>().ok().and_then(|n| n.checked_sub(1)),
        PyValue::Bool(b) => Some(i64::from(*b) - 1),
        PyValue::Float(f) => {
            let PyValue::Int(ref decimal) = py_int(&PyValue::Float(f - 1.0)).map_err(|_| {
                StateError::raised("OverflowError", "cannot convert float infinity to integer")
            })?
            else {
                unreachable!("int(float) returns an integer")
            };
            decimal.parse::<i64>().ok()
        }
        _ => None,
    }
    .ok_or_else(|| {
        StateError::raised(
            "OverflowError",
            "Python int too large to convert to SQLite INTEGER",
        )
    })?;
    let row = first_log_row(conn, "WHERE seq > ? ORDER BY seq", &[&lower])?;
    Ok(
        if row.is_some_and(|r| {
            cmp_keys(&h.seq, &PyValue::int(i128::from(r.seq))) == Some(Ordering::Equal)
                && same_mac(&h.mac, &cps(&r.mac))
        }) {
            HeadStatus::Stale
        } else {
            HeadStatus::Mismatch
        },
    )
}

#[derive(Clone, Debug)]
pub struct KeyReadError {
    pub path: PathBuf,
    pub errno: Option<i32>,
    pub reason: PyStr,
}
impl From<KeyUnreadable> for KeyReadError {
    fn from(e: KeyUnreadable) -> Self {
        Self {
            path: e.path,
            errno: e.errno,
            reason: cps(&e.reason),
        }
    }
}

/// The observation retained by the opener. The private key never appears in Debug.
#[derive(Clone)]
pub struct KeyState {
    pub state: KeyStatus,
    pub keyring: KeyFiles,
    pub store_file: PathBuf,
    pub expected: PyStr,
    pub key: Option<Key>,
    pub error: Option<KeyReadError>,
    pub bound_to: Option<PathBuf>,
    pub unread: Option<PyStr>,
    pub unfit: Option<RingFit>,
}
impl fmt::Debug for KeyState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("KeyState")
            .field("state", &self.state)
            .field("keyring", &self.keyring)
            .field("store_file", &self.store_file)
            .field("expected", &self.expected)
            .field("error", &self.error)
            .field("bound_to", &self.bound_to)
            .field("unread", &self.unread)
            .field("unfit", &self.unfit)
            .finish()
    }
}

fn bound_words(
    dir: &[u32],
    binding: &[u32],
    store_id: &[u32],
    other: &[u32],
    unread: Option<&[u32]>,
) -> (PyStr, PyStr) {
    let holds = match unread {
        Some(why) => words(
            "which could not be read to tell whether it still holds store id {} ({})",
            &[store_id, why],
        ),
        None => words("which still holds store id {}", &[store_id]),
    };
    let summary = words(
        "the key ring under {} is bound elsewhere: it belongs to the store at {}, {}; every \
            open of this file is read-only, and nothing is rekeyed or written",
        &[dir, other, &holds],
    );
    let remedy = words(
        "move or remove one of the two stores, or run `bettermemory key adopt --yes` with \
            BETTERMEMORY_DIR naming this file's directory, which binds the key ring to this one on \
            the record (it rewrites {})",
        &[binding],
    );
    (summary, remedy)
}

impl KeyState {
    pub fn new(state: KeyStatus, keyring: KeyFiles, store_file: PathBuf, expected: PyStr) -> Self {
        Self {
            state,
            keyring,
            store_file,
            expected,
            key: None,
            error: None,
            bound_to: None,
            unread: None,
            unfit: None,
        }
    }
    pub fn key_path(&self) -> PathBuf {
        lexical_path(&self.keyring.current_path())
    }
    pub fn withheld(&self) -> Option<PathBuf> {
        match self.state {
            KeyStatus::BoundElsewhere => self.bound_to.as_deref().map(lexical_path),
            KeyStatus::Unclaimed => Some(lexical_path(&self.keyring.binding_path())),
            _ => None,
        }
    }
    pub fn held(&self) -> Option<String> {
        self.key.as_ref().map(|k| fingerprint(k.bytes()))
    }
    fn history(&self) -> Result<PyStr, StateError> {
        let mut marks = Vec::new();
        if let Some(fit) = &self.unfit {
            if let Some(seq) = fit.origin.seq {
                marks.push(cps(&format!(
                    "carries its origin anchor's row at seq {seq}"
                )))
            }
            if let Some(h) = &fit.head {
                marks.push(words(
                    "reaches its head checkpoint at seq {}",
                    &[&display_seq(&h.seq)?],
                ))
            }
        }
        Ok(if marks.is_empty() {
            cps("carries the history it records")
        } else {
            marks.join(cps(" and ").as_slice())
        })
    }
    /// Retain the Python Path spellings alongside the filesystem observation.
    pub fn spelled<'a>(
        &'a self,
        store_file: &[u32],
        keys_dir: &[u32],
        bound_to: Option<&[u32]>,
    ) -> KeyStateView<'a> {
        KeyStateView {
            facts: self,
            store_file: normalize_spelling(store_file),
            keys_dir: normalize_spelling(keys_dir),
            bound_to: bound_to.map(normalize_spelling),
        }
    }

    fn native_view(&self) -> KeyStateView<'_> {
        KeyStateView {
            facts: self,
            store_file: path_text(&self.store_file),
            keys_dir: path_text(self.keyring.keys_dir()),
            bound_to: self.bound_to.as_deref().map(path_text),
        }
    }

    pub fn summary(&self) -> Result<PyStr, StateError> {
        self.native_view().summary()
    }
    pub fn remedy(&self) -> Result<Option<PyStr>, StateError> {
        self.native_view().remedy()
    }
    pub fn refusal(&self, path: &Path) -> Result<PyStr, StateError> {
        self.native_view().refusal(&path_text(path))
    }
    pub fn details(&self) -> Result<PyValue, StateError> {
        self.native_view().details()
    }
}

/// Text returned by KeyState uses the supplied Path spelling even where two
/// distinct Python strings project to identical host bytes.
pub struct KeyStateView<'a> {
    facts: &'a KeyState,
    pub store_file: PyStr,
    keys_dir: PyStr,
    bound_to: Option<PyStr>,
}

impl KeyStateView<'_> {
    fn ring_file(&self, suffix: &str) -> PyStr {
        ring_file(&self.keys_dir, self.facts.keyring.store_id(), suffix)
    }

    pub fn withheld(&self) -> Option<PyStr> {
        match self.facts.state {
            KeyStatus::BoundElsewhere => self.bound_to.clone(),
            KeyStatus::Unclaimed => Some(self.ring_file(".path")),
            _ => None,
        }
    }
    fn bound(&self) -> Result<(PyStr, PyStr), StateError> {
        let other = self
            .bound_to
            .as_deref()
            .ok_or_else(|| StateError::raised("AssertionError", ""))?;
        Ok(bound_words(
            &self.keys_dir,
            &self.ring_file(".path"),
            &cps(self.facts.keyring.store_id()),
            other,
            self.facts.unread.as_deref(),
        ))
    }
    pub fn summary(&self) -> Result<PyStr, StateError> {
        let path = self.ring_file(".key");
        let expected = first16(&self.facts.expected);
        Ok(match self.facts.state {
            KeyStatus::Present => words(
                "the key at {} matches the store (fingerprint {})",
                &[&path, expected],
            ),
            KeyStatus::Unreadable => {
                let unknown = cps("unknown");
                let reason = self.facts
                    .error
                    .as_ref()
                    .map_or(unknown.as_slice(), |e| e.reason.as_slice());
                words(
                    "the key at {} is there and this account cannot read it ({}): the log cannot be \
                        verified here, and every owner open (the daemon, a hook, the CLI) fails until it can; \
                        nothing is rekeyed over it",
                    &[&path, reason],
                )
            }
            KeyStatus::Missing => words(
                "no key for this store at {}: the log cannot be verified here, and the next owner open \
                    (the daemon, a hook, the CLI) writes a new key and rekeys the log, so the rows signed \
                    before it stay unverifiable",
                &[&path],
            ),
            KeyStatus::Foreign => {
                let held = cps(&self.facts.held().unwrap_or_default());
                words(
                    "the key at {} (fingerprint {}) is not the store's (fingerprint {}): the log cannot be \
                        verified here, and the next owner open keeps it only if it signs a segment of the log, \
                        and otherwise retires it, writes a new key and rekeys the log",
                    &[&path, first16(&held), expected],
                )
            }
            KeyStatus::Unclaimed => {
                let why = match &self.facts.unfit {
                    Some(fit) => fit.reason()?,
                    None => None,
                };
                let detail = why.map_or_else(Vec::new, |r| words(" ({})", &[&r]));
                words(
                    "the key ring under {} is bound to no store file yet and holds a history this file does \
                        not carry{}: it is bound only to the file that carries it, so every open of this file \
                        is read-only, and nothing is rekeyed or written",
                    &[&self.keys_dir, &detail],
                )
            }
            KeyStatus::BoundElsewhere => self.bound()?.0,
        })
    }
    pub fn remedy(&self) -> Result<Option<PyStr>, StateError> {
        Ok(Some(match self.facts.state {
            KeyStatus::Present => return Ok(None),
            KeyStatus::Unreadable => cps("make it readable to this account again (owner-only, 0600), and the volume a link to it \
                points into mounted"),
            KeyStatus::Missing => words(
                "file the store's key back with `bettermemory key import FILE` (a `bettermemory key \
                    export` of the keys directory that held it, or its key file {} from where it was \
                    kept), or point BETTERMEMORY_KEYS_DIR at the directory that holds it, before anything \
                    opens the store for writing",
                &[&fsdecode(
                    self.facts.key_path().file_name().expect("key filename"),
                )],
            ),
            KeyStatus::Foreign => cps("file the key whose fingerprint the store records back with `bettermemory key import \
                FILE`; `bettermemory log verify` shows which segments verify"),
            KeyStatus::Unclaimed => words(
                "this key ring belongs with the store file whose log {} (`bettermemory key show` in a \
                    store's directory says whether it is that file): open that store first, whose first \
                    writing open binds the ring to it, or run `bettermemory key adopt --yes` with \
                    BETTERMEMORY_DIR naming its directory, which binds the ring to it on the record and \
                    refuses a file that does not carry that history",
                &[&self.facts.history()?],
            ),
            KeyStatus::BoundElsewhere => self.bound()?.1,
        }))
    }
    pub fn refusal(&self, path: &[u32]) -> Result<PyStr, StateError> {
        let remedy = self
            .remedy()?
            .unwrap_or_else(|| cps("open it from the process that owns the store"));
        let owner = if matches!(self.facts.state, KeyStatus::Missing | KeyStatus::Foreign) {
            " If the key is lost for good, open it with allow_rekey=True from the process that owns \
                the store, which writes a new key and starts a new chain segment."
        } else {
            ""
        };
        Ok(words(
            "store {} is open for reading only: {}. {}.{}",
            &[
                &normalize_spelling(path),
                &self.summary()?,
                &sentence(&remedy),
                &cps(owner),
            ],
        ))
    }
    pub fn details(&self) -> Result<PyValue, StateError> {
        let mut out = vec![
            item("key_state", PyValue::str(self.facts.state.name())),
            item("key_path", PyValue::Str(self.ring_file(".key"))),
            item(
                "expected_fingerprint",
                PyValue::Str(self.facts.expected.clone()),
            ),
        ];
        if let Some(k) = self.facts.held() {
            out.push(item("held_fingerprint", PyValue::str(&k)));
        }
        if let Some(e) = &self.facts.error {
            out.push(item("reason", PyValue::Str(e.reason.clone())));
        }
        if let Some(other) = &self.bound_to {
            out.push(item("bound_to", PyValue::Str(other.clone())));
        }
        if self.bound_to.is_some() || self.facts.unfit.is_some() {
            out.push(item("binding_path", PyValue::Str(self.ring_file(".path"))));
        }
        if let Some(fit) = &self.facts.unfit {
            out.push(item("ring_fit", fit.to_dict()?));
        }
        Ok(PyValue::Dict(out))
    }
}

fn guard_path(path: &[u32]) -> Result<PyStr, StateError> {
    match resolve_spelling(path) {
        Err(
            StateError::Read(StoreError::Io(_))
            | StateError::Raised {
                class: "RuntimeError",
                ..
            },
        ) => Ok(normalize_spelling(path)),
        result => result,
    }
}

static WARNED: LazyLock<Mutex<HashSet<(PyStr, PyStr)>>> =
    LazyLock::new(|| Mutex::new(HashSet::new()));
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    Warning,
    Debug,
}
fn level(pair: (PyStr, PyStr)) -> Level {
    #[cfg(windows)]
    let pair = (
        crate::unicode::lower(&pair.0),
        crate::unicode::lower(&pair.1),
    );
    let first = WARNED
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .insert(pair);
    if first {
        Level::Warning
    } else {
        Level::Debug
    }
}

#[derive(Debug)]
pub struct Warning<'a> {
    pub level: Level,
    pub path: &'a Path,
    pub summary: PyStr,
    pub remedy: PyStr,
}
impl Warning<'_> {
    pub const LOGGER: &'static str = "bettermemory.store";
    pub const FORMAT: &'static str = "store %s: %s. %s";
    pub fn message(&self) -> PyStr {
        words(
            "store {}: {}. {}",
            &[&path_text(self.path), &self.summary, &self.remedy],
        )
    }
}
#[derive(Debug)]
pub enum WarningError<E> {
    State(StateError),
    Logger(E),
}
impl<E> From<StateError> for WarningError<E> {
    fn from(e: StateError) -> Self {
        Self::State(e)
    }
}

pub fn warn_bound_elsewhere<E>(
    ring: &KeyFiles,
    path: &Path,
    store_id: &[u32],
    other: &Path,
    unread: Option<&[u32]>,
    emit: impl FnOnce(Warning<'_>) -> Result<(), E>,
) -> Result<(), WarningError<E>> {
    let dir = path_text(ring.keys_dir());
    let other = path_text(other);
    let level = level((guard_path(&path_text(path))?, guard_path(&other)?));
    let (summary, remedy) = bound_words(
        &dir,
        &ring_file(&dir, ring.store_id(), ".path"),
        store_id,
        &other,
        unread,
    );
    emit(Warning {
        level,
        path,
        summary,
        remedy: sentence(&remedy),
    })
    .map_err(WarningError::Logger)
}

pub(crate) fn warn_bound_exact<E>(
    state: &KeyState,
    store: &[u32],
    dir: &[u32],
    bound: &[u32],
    emit: impl FnOnce(Level, PyStr, PyStr) -> Result<(), E>,
) -> Result<(), WarningError<E>> {
    let view = state.spelled(store, dir, Some(bound));
    let other = view.bound_to.as_deref().expect("bound path supplied");
    let level = level((guard_path(&view.store_file)?, guard_path(other)?));
    let (summary, remedy) = view.bound()?;
    emit(level, summary, sentence(&remedy)).map_err(WarningError::Logger)
}

pub(crate) fn warn_unclaimed_exact<E>(
    state: &KeyState,
    store: &[u32],
    dir: &[u32],
    emit: impl FnOnce(Level, PyStr, PyStr) -> Result<(), E>,
) -> Result<(), WarningError<E>> {
    let view = state.spelled(store, dir, None);
    unclaimed(&view, emit)
}

fn unclaimed<E>(
    view: &KeyStateView<'_>,
    emit: impl FnOnce(Level, PyStr, PyStr) -> Result<(), E>,
) -> Result<(), WarningError<E>> {
    let level = level((guard_path(&view.store_file)?, view.ring_file(".path")));
    let remedy = sentence(&view.remedy()?.unwrap_or_default());
    emit(level, view.summary()?, remedy).map_err(WarningError::Logger)
}

pub fn warn_unclaimed<E>(
    state: &KeyState,
    emit: impl FnOnce(Warning<'_>) -> Result<(), E>,
) -> Result<(), WarningError<E>> {
    unclaimed(&state.native_view(), |level, summary, remedy| {
        emit(Warning {
            level,
            path: &state.store_file,
            summary,
            remedy,
        })
    })
}
