//! The key ring as observed through an already opened SQLite connection.
mod diagnostic;
pub mod open_reader;
use super::key_state::{
    first_log_row, head_status, KeyState, KeyStateView, KeyStatus, RingFit, StateError,
    WarningError,
};
use super::rotation;
use crate::fsutil::fsdecode;
use crate::log::ring::binding_path::{BindingError, BindingPath, HOST};
use crate::log::ring::{validated_store_id, BadId, KeyFiles};
use crate::log::{check_origin_with_keys, fingerprint, Head, Key, KeyRing};
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::unicode::{self, cps};
use diagnostic::{os_cause, read_failure};
pub use diagnostic::{KeyLog, LogCause, LogLevel};
use rusqlite::Connection;
use std::borrow::Cow;
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;

fn ring_json_error(error: PyError, file: Option<&[u8]>) -> StateError {
    let message = match error {
        PyError::OverflowError => "cannot convert float infinity to integer",
        PyError::RecursionError => {
            let text = std::str::from_utf8(file.unwrap_or_default()).unwrap_or_default();
            crate::json::reading_recursion_message(&cps(&text
                .replace("\r\n", "\n")
                .replace('\r', "\n")))
        }
        _ => error.name(),
    };
    StateError::Raised {
        class: error.name(),
        message: cps(message),
    }
}

fn read_origin(ring: &KeyFiles) -> Result<Option<crate::log::HistoryOrigin>, StateError> {
    let files = KeyRing {
        origin_file: fs::read(ring.origin_path()).ok(),
        ..KeyRing::default()
    };
    files
        .read_origin()
        .map_err(|e| ring_json_error(e, files.origin_file.as_deref()))
}

fn read_head(ring: &KeyFiles) -> Result<Option<Head>, StateError> {
    let files = KeyRing {
        head_file: fs::read(ring.head_path()).ok(),
        ..KeyRing::default()
    };
    files
        .read_head()
        .map_err(|e| ring_json_error(e, files.head_file.as_deref()))
}

fn held_keys(ring: &KeyFiles, supplied: Option<&Key>) -> Vec<Key> {
    let mut keys: Vec<Key> = ring.load_current().ok().flatten().into_iter().collect();
    let prefix = cps(&format!("{}.", ring.store_id()));
    let suffix = cps(".key");
    if let Ok(entries) = fs::read_dir(ring.keys_dir()) {
        let mut paths = Vec::new();
        for entry in entries.flatten() {
            let name = fsdecode(&entry.file_name());
            let name = crate::gitfile_rule::hostpath::text::normcase(HOST, &name);
            if name.len() >= prefix.len() + suffix.len()
                && name.starts_with(&prefix)
                && name.ends_with(&suffix)
            {
                paths.push((name, entry.path()));
            }
        }
        paths.sort_by(|a, b| a.0.cmp(&b.0));
        keys.extend(
            paths
                .into_iter()
                .filter_map(|(_, p)| fs::read(p).ok().map(Key::new)),
        );
    }
    // Keep discovery order without copying key bytes into an unwiped hash table.
    let mut seen = HashSet::new();
    if let Some(key) = supplied {
        seen.insert(key.bytes());
    }
    let keep: Vec<bool> = keys.iter().map(|k| seen.insert(k.bytes())).collect();
    keys.into_iter()
        .zip(keep)
        .filter_map(|(key, fresh)| fresh.then_some(key))
        .collect()
}

/// Compare the caller's physical ring to the log on its own connection.
/// A held signer skips the current-key file, directory scan and retired files.
pub fn ring_fit(
    conn: &Connection,
    ring: &KeyFiles,
    current_key: Option<&Key>,
) -> Result<RingFit, StateError> {
    let origin = read_origin(ring)?;
    let row = match &origin {
        Some(anchor) => first_log_row(conn, "WHERE seq = ?", &[&anchor.seq])?,
        None => None,
    };
    let keys = current_key.into_iter().map(Cow::Borrowed).chain(
        std::iter::once_with(|| held_keys(ring, current_key))
            .flatten()
            .map(Cow::Owned),
    );
    let origin = check_origin_with_keys(
        origin.as_ref(),
        row.as_ref(),
        keys,
        crate::log::row_verifies,
    );
    let head = read_head(ring)?;
    let head_status = head_status(head.as_ref(), conn)?;
    let log_end = first_log_row(conn, "ORDER BY seq DESC LIMIT 1", &[])?;
    Ok(RingFit {
        origin,
        head,
        head_status,
        log_end,
    })
}

/// A failed physical use of an otherwise valid Python Path. The full
/// filename, including the keyring suffix, is retained on a codec failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FilePathError {
    Encoding {
        object: PyStr,
        start: usize,
        end: usize,
    },
    Nul,
}

impl FilePathError {
    pub fn class(&self) -> &'static str {
        match self {
            Self::Encoding { .. } => "UnicodeEncodeError",
            Self::Nul => "ValueError",
        }
    }
    /// UnicodeEncodeError.encoding and .reason when conversion to a POSIX
    /// filename fails. The owning input and rejected range are in Encoding.
    pub fn codec(&self) -> Option<(&'static str, &'static str)> {
        match self {
            Self::Encoding { .. } => Some(("utf-8", "surrogates not allowed")),
            Self::Nul => None,
        }
    }
    pub fn message(&self) -> PyStr {
        match self {
            Self::Nul => cps("embedded null byte"),
            Self::Encoding { object, start, end } => {
                let position = if *end == start + 1 {
                    format!("character '\\u{:04x}' in position {start}", object[*start])
                } else {
                    format!("characters in position {start}-{}", end - 1)
                };
                cps(&format!(
                    "'utf-8' codec can't encode {position}: surrogates not allowed"
                ))
            }
        }
    }
}

pub(crate) fn os_path(text: &[u32]) -> Result<PathBuf, FilePathError> {
    #[cfg(unix)]
    let host = {
        use crate::response_write::{fsencode_as, FsEncoding};
        use std::os::unix::ffi::OsStringExt;
        let bytes = fsencode_as(text, FsEncoding::SurrogateEscape).ok_or_else(|| {
            let start = text
                .iter()
                .position(|&u| char::from_u32(u).is_none() && !(0xdc80..=0xdcff).contains(&u))
                .expect("fsencode rejected an unencodable codepoint");
            let end = start
                + 1
                + text[start + 1..]
                    .iter()
                    .take_while(|&&u| (0xd800..=0xdfff).contains(&u))
                    .count();
            FilePathError::Encoding {
                object: text.to_vec(),
                start,
                end,
            }
        })?;
        PathBuf::from(std::ffi::OsString::from_vec(bytes))
    };
    #[cfg(windows)]
    let host = {
        use std::os::windows::ffi::OsStringExt;
        let mut wide = Vec::new();
        for &cp in text {
            if let Ok(unit) = u16::try_from(cp) {
                wide.push(unit);
            } else if let Some(ch) = char::from_u32(cp) {
                wide.extend_from_slice(ch.encode_utf16(&mut [0; 2]));
            }
        }
        PathBuf::from(std::ffi::OsString::from_wide(&wide))
    };
    if text.contains(&0) {
        Err(FilePathError::Nul)
    } else {
        Ok(host)
    }
}

/// The caller's selected directory and store id, retained before any file
/// lookup. Distinct Python spellings can denote identical host bytes.
#[derive(Clone, Debug)]
pub struct ReaderRing {
    pub keys_dir: PyStr,
    store_id: String,
}
impl ReaderRing {
    pub fn new(keys_dir: &[u32], store_id: &PyValue) -> Result<Self, BadId> {
        Ok(Self {
            keys_dir: super::key_state::normalize_spelling(keys_dir),
            store_id: validated_store_id(store_id)?,
        })
    }
    pub fn from_files(files: &KeyFiles) -> Self {
        Self {
            keys_dir: super::key_state::normalize_spelling(&fsdecode(files.keys_dir().as_os_str())),
            store_id: files.store_id().to_owned(),
        }
    }
    pub fn store_id(&self) -> &str {
        &self.store_id
    }
    fn file(&self, suffix: &str) -> PyStr {
        super::key_state::ring_file(&self.keys_dir, &self.store_id, suffix)
    }
    fn files(&self) -> Result<KeyFiles, FilePathError> {
        let dir = os_path(&self.keys_dir)?;
        Ok(KeyFiles::new(&dir, &PyValue::str(&self.store_id)).expect("validated store id"))
    }

    /// Inspect the physical side files on demand and the caller's active SQLite view.
    pub fn ring_fit(
        &self,
        conn: &Connection,
        current_key: Option<&Key>,
    ) -> Result<RingFit, StateError> {
        match self.files() {
            Ok(ring) => ring_fit(conn, &ring, current_key),
            // read_origin and read_head each treat a filename ValueError as absent.
            Err(_) => Ok(RingFit {
                origin: crate::log::check_origin(None, None, &[]),
                head: None,
                head_status: head_status(None, conn)?,
                log_end: first_log_row(conn, "ORDER BY seq DESC LIMIT 1", &[])?,
            }),
        }
    }
}

#[derive(Debug)]
pub enum ReadError<E> {
    Path(FilePathError),
    State(StateError),
    Logger(E),
}
impl<E> From<StateError> for ReadError<E> {
    fn from(e: StateError) -> Self {
        Self::State(e)
    }
}
impl<E> From<FilePathError> for ReadError<E> {
    fn from(e: FilePathError) -> Self {
        Self::Path(e)
    }
}
impl<E> From<WarningError<E>> for ReadError<E> {
    fn from(e: WarningError<E>) -> Self {
        match e {
            WarningError::State(e) => Self::State(e),
            WarningError::Logger(e) => Self::Logger(e),
        }
    }
}
/// Source spellings alongside the native state and its retained key. Use the
/// view when rendering; POSIX can give different Python paths identical bytes.
#[derive(Clone, Debug)]
pub struct PhysicalKeyState {
    pub state: KeyState,
    pub store_file: PyStr,
    pub keys_dir: PyStr,
    pub binding: Option<BindingPath>,
    /// A warning callback and the returned unreadable state share this exception.
    pub error: Option<Arc<LogCause>>,
}
impl PhysicalKeyState {
    pub fn view(&self) -> KeyStateView<'_> {
        self.state.spelled(
            &self.store_file,
            &self.keys_dir,
            self.binding.as_ref().map(BindingPath::text),
        )
    }
}

fn binding_failure(e: BindingError) -> StateError {
    match e {
        BindingError::Recursion(m) => StateError::Raised {
            class: "RecursionError",
            message: cps(m),
        },
        BindingError::Value => StateError::Raised {
            class: "ValueError",
            message: cps("embedded null byte"),
        },
        BindingError::Os(e) => StateError::Read(super::StoreError::Io(e)),
    }
}
impl ReaderRing {
    fn unclaimed(
        &self,
        conn: &Connection,
        ring: &KeyFiles,
        key: Option<&Key>,
    ) -> Result<Option<RingFit>, StateError> {
        match ring.read_binding_exact() {
            Ok(Some(_)) | Err(BindingError::Os(_)) => return Ok(None),
            Ok(None) => {}
            Err(e) => return Err(binding_failure(e)),
        }
        if read_origin(ring)?.is_none() && read_head(ring)?.is_none() {
            return Ok(None);
        }
        match ring_fit(conn, ring, key) {
            Ok(fit) if !fit.fits() => Ok(Some(fit)),
            Ok(_)
            | Err(StateError::Read(super::StoreError::Sqlite(_)))
            | Err(StateError::Raised {
                class: "OperationalError",
                ..
            }) => Ok(None),
            Err(e) => Err(e),
        }
    }
    /// Classify a ring for an existing connection, without claiming a binding.
    /// The native store path and its original Python spelling come from the
    /// same already-opened store. The callback runs at the original log call.
    pub fn key_state_at<E>(
        &self,
        conn: &Connection,
        store_file: &Path,
        spelling: &[u32],
        expected: &[u32],
        warn: bool,
        mut emit: impl FnMut(KeyLog) -> Result<(), E>,
    ) -> Result<PhysicalKeyState, ReadError<E>> {
        // finish_rotation first tries the complete pending filename. Encoding
        // the directory alone would report the wrong UnicodeEncodeError.object.
        os_path(&self.file(".key.pending"))?;
        let ring = self.files()?;
        let source = super::key_state::normalize_spelling(spelling);
        let recorded = unicode::to_string(expected).unwrap_or_default();
        rotation::finish_with(
            &ring,
            &recorded,
            cfg!(windows),
            &mut |s| std::thread::sleep(std::time::Duration::from_secs_f64(s)),
            || {
                let bound = match ring.read_binding_exact() {
                    Ok(None) => return Ok(true),
                    Ok(Some(p)) => p,
                    Err(BindingError::Os(_)) => return Ok(false),
                    Err(e) => return Err(ReadError::State(binding_failure(e))),
                };
                let resolved = match super::key_state::resolve_spelling(&source) {
                    Ok(p) => p,
                    Err(StateError::Read(super::StoreError::Io(_))) => return Ok(false),
                    Err(e) => return Err(ReadError::State(e)),
                };
                Ok(BindingPath::parse(HOST, &resolved).is_some_and(|p| bound.same_file(&p)))
            },
            |n| emit(self.rotation_log(&source, expected, n)).map_err(ReadError::Logger),
        )?;
        let mut state = KeyState::new(
            KeyStatus::Present,
            ring.clone(),
            store_file.to_path_buf(),
            expected.to_vec(),
        );
        match ring.load_current_exact() {
            Ok(k) => state.key = k,
            Err(e) => {
                state.state = KeyStatus::Unreadable;
                let error = read_failure(&e);
                let cause = Arc::new(self.unreadable(&error, e.cause));
                if warn {
                    emit(KeyLog {
                        level: LogLevel::Warning,
                        format: "store %s opens without its key: %s",
                        args: vec![source.clone(), cause.message.clone()],
                        cause: Some(Arc::clone(&cause)),
                    })
                    .map_err(ReadError::Logger)?;
                }
                state.error = Some(error);
                return Ok(PhysicalKeyState {
                    state,
                    store_file: source,
                    keys_dir: self.keys_dir.clone(),
                    binding: None,
                    error: Some(cause),
                });
            }
        }
        state.unfit = self.unclaimed(conn, &ring, state.key.as_ref())?;
        let log_state = |level, summary, remedy| {
            let level = match level {
                super::key_state::Level::Warning => LogLevel::Warning,
                super::key_state::Level::Debug => LogLevel::Debug,
            };
            KeyLog {
                level,
                format: "store %s: %s. %s",
                args: vec![source.clone(), summary, remedy],
                cause: None,
            }
        };
        let mut binding = None;
        if state.unfit.is_some() {
            state.state = KeyStatus::Unclaimed;
            if warn {
                super::key_state::warn_unclaimed_exact(
                    &state,
                    &source,
                    &self.keys_dir,
                    |l, s, r| emit(log_state(l, s, r)),
                )?;
            }
        } else if let Some((other, unread)) = elsewhere(&ring, &source)? {
            state.bound_to = other.host_path();
            state.unread = unread;
            state.state = KeyStatus::BoundElsewhere;
            if warn {
                super::key_state::warn_bound_exact(
                    &state,
                    &source,
                    &self.keys_dir,
                    other.text(),
                    |l, s, r| emit(log_state(l, s, r)),
                )?;
            }
            binding = Some(other);
        } else {
            state.state = match &state.key {
                None => KeyStatus::Missing,
                Some(key) if fingerprint(key.bytes()) != recorded => KeyStatus::Foreign,
                Some(_) => KeyStatus::Present,
            };
        }
        Ok(PhysicalKeyState {
            state,
            store_file: source,
            keys_dir: self.keys_dir.clone(),
            binding,
            error: None,
        })
    }
}

type BoundState = (BindingPath, Option<PyStr>);
fn elsewhere(ring: &KeyFiles, store: &[u32]) -> Result<Option<BoundState>, StateError> {
    let previous = match ring.read_binding_exact() {
        Ok(p) => p,
        Err(BindingError::Os(_)) => return Ok(None),
        Err(e) => return Err(binding_failure(e)),
    };
    let resolved = match super::key_state::resolve_spelling(store) {
        Ok(p) => p,
        Err(StateError::Read(super::StoreError::Io(_))) => return Ok(None),
        Err(e) => return Err(e),
    };
    let Some(previous) = previous else {
        return Ok(None);
    };
    if BindingPath::parse(HOST, &resolved).is_some_and(|p| previous.same_file(&p)) {
        return Ok(None);
    }
    let Some(path) = previous.host_path() else {
        return Ok(None);
    };
    let unread = match super::open::store_id_at(&path, &previous) {
        Ok(id) if id.as_deref() != Some(ring.store_id()) => return Ok(None),
        Ok(_) => None,
        Err(super::open::SecondaryError::Reason(message)) => Some(message),
        Err(super::open::SecondaryError::Stat(e)) => Some(
            os_cause(
                e,
                Some(previous.text().to_vec()),
                None,
                rotation::Call::Win32,
            )
            .message,
        ),
    };
    Ok(Some((previous, unread)))
}

/// Native-path convenience for an admitted store and an existing physical ring.
pub fn key_state_on<E>(
    conn: &Connection,
    ring: &KeyFiles,
    store_file: &Path,
    expected: &[u32],
    warn: bool,
    emit: impl FnMut(KeyLog) -> Result<(), E>,
) -> Result<PhysicalKeyState, ReadError<E>> {
    ReaderRing::from_files(ring).key_state_at(
        conn,
        store_file,
        &fsdecode(store_file.as_os_str()),
        expected,
        warn,
        emit,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::log::{compute_mac, row_verifies, HistoryOrigin, LogRow};

    #[test]
    fn authenticating_the_held_signer_takes_one_hmac_and_no_fallback() {
        let held = Key::new(vec![17; 32]);
        let mut row = LogRow {
            seq: 1,
            ts: "2025-01-02T03:04:05Z".into(),
            session: None,
            kind: "search".into(),
            payload: "{}".into(),
            prev_mac: String::new(),
            mac: String::new(),
        };
        row.mac = compute_mac(
            held.bytes(),
            row.seq,
            &row.ts,
            None,
            &row.kind,
            &row.payload,
            &row.prev_mac,
        );
        let anchor = HistoryOrigin {
            seq: 1,
            mac: row.mac.clone(),
            key_fingerprint: Some(fingerprint(held.bytes())),
        };
        for (stored, expected) in [(None, 0), (Some(&row), 1)] {
            let keys = std::iter::once(&held)
                .chain(std::iter::from_fn(|| panic!("the fallback must stay lazy")));
            let mut hmacs = 0;
            let check = check_origin_with_keys(Some(&anchor), stored, keys, |key, row| {
                hmacs += 1;
                row_verifies(key, row)
            });
            assert_eq!(hmacs, expected);
            assert_eq!(check.signer.is_some(), stored.is_some());
        }
    }
}
