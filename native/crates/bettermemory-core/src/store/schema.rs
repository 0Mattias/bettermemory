//! The additive schema steps of `store_schema.py` (`_add_membership_tables`,
//! `_add_returned_marks`, `_add_proposals_index`, `_add_session_index`,
//! `_add_path_targets`) and its leaves `absent_text`, `orphan_names`,
//! `refuse_unmigrated_v8` and `holds_v8_files`.
//!
//! Each step takes the connection, the store's path as the log would print
//! it and meta as the open read it; the membership store id comes from the
//! [`Provider`], drawn only when something is wanted. The reference logs a
//! read-only store's warning; here it is returned as a value, in order.
//! Nothing here is wired into the writing open yet (`open.rs` still
//! refuses such a store as `NotPorted::MembershipStep`).

use rusqlite::Connection;
use std::io;

use super::admission;
use super::create::{Provider, MEMBERSHIP_STORE_ID};
use super::{sql_value, SqlValue, StoreError};
use crate::fsdata::Fs;
use crate::json;
use crate::pyvalue::{self, PyError, PyStr, PyValue};
use crate::unicode;

/// `RETURNED_BACKFILL_KEY`.
pub const RETURNED_BACKFILL_KEY: &str = "returned_backfill";

/// The optional columns of `_MEMBERSHIP_SPECS`, in its order: only
/// `checkouts` has one.
const OPTIONAL: [(&str, &[&str]); 1] = [("checkouts", &["unresolved_json"])];

/// Why a step stopped: what the reference raises out of it.
#[derive(Debug)]
pub enum StepError {
    /// A `sqlite3` error other than a read-only store's: its class
    /// (`OperationalError`, `IntegrityError`, ...) and its text.
    Sqlite {
        class: &'static str,
        message: String,
    },
    /// `secrets.token_hex` failed (the provider's draw).
    Mint(io::Error),
    /// The meta row of the store id was not there after its insert
    /// (`fetchone()` gave None: a TypeError in the reference).
    NoStoreId,
    /// A read of `sqlite_master` the store's reader refused.
    Read(StoreError),
}

impl From<StoreError> for StepError {
    fn from(e: StoreError) -> StepError {
        StepError::Read(e)
    }
}

/// `sqlite3`'s exception class for a result code (`_pysqlite_seterror`).
fn class_of(code: i32) -> &'static str {
    match code & 0xff {
        2 | 12 => "InternalError",
        7 => "MemoryError",
        1 | 3 | 4 | 5 | 6 | 8 | 9 | 10 | 13 | 14 | 15 | 16 | 17 => "OperationalError",
        11 | 18 => "DatabaseError",
        19 | 20 => "IntegrityError",
        21 | 25 => "InterfaceError",
        _ => "DatabaseError",
    }
}

/// A rusqlite error as the reference raises it.
fn raised(e: rusqlite::Error) -> StepError {
    let (code, message) = match &e {
        rusqlite::Error::SqlInputError { error, msg, .. } => (error.extended_code, msg.clone()),
        rusqlite::Error::SqliteFailure(f, m) => {
            (f.extended_code, m.clone().unwrap_or_else(|| f.to_string()))
        }
        other => (1, other.to_string()),
    };
    StepError::Sqlite {
        class: class_of(code),
        message,
    }
}

/// `Ok(())` for an OperationalError whose text, spaces removed and
/// lowercased, holds `readonly` (the step's read-only store); the raised
/// error otherwise.
fn sorted_error(e: rusqlite::Error) -> Result<(), StepError> {
    match raised(e) {
        StepError::Sqlite { class, message }
            if class == "OperationalError"
                && message.replace(' ', "").to_lowercase().contains("readonly") =>
        {
            Ok(())
        }
        other => Err(other),
    }
}

/// `BEGIN IMMEDIATE`, `run`, `COMMIT`; a rollback when any fails while a
/// transaction is open. `Ok(false)` when the store is read-only.
fn in_write(
    conn: &Connection,
    run: impl FnOnce(&Connection) -> rusqlite::Result<()>,
) -> Result<bool, StepError> {
    let done = conn.execute_batch("BEGIN IMMEDIATE").and_then(|()| {
        let inner = run(conn).and_then(|()| conn.execute_batch("COMMIT"));
        if inner.is_err() && !conn.is_autocommit() {
            conn.execute_batch("ROLLBACK")?;
        }
        inner
    });
    match done {
        Ok(()) => Ok(true),
        Err(e) => sorted_error(e).map(|()| false),
    }
}

fn master(conn: &Connection) -> Result<Vec<(String, String)>, StepError> {
    let failed = raised;
    let mut statement = conn
        .prepare("SELECT name, sql FROM sqlite_master WHERE name IS NOT NULL")
        .map_err(failed)?;
    let mut rows = statement.query([]).map_err(failed)?;
    let mut out = Vec::new();
    while let Some(row) = rows.next().map_err(failed)? {
        let name = sql_value(row.get_ref(0).map_err(failed)?)?.py_str();
        let sql = match sql_value(row.get_ref(1).map_err(failed)?)? {
            SqlValue::Null => String::new(),
            v => v.py_str(),
        };
        out.push((name, sql));
    }
    Ok(out)
}

/// `statement.strip() for statement in text.split(";") if statement.strip()`.
fn statements(text: &str) -> Vec<&str> {
    text.split(';')
        .map(|s| s.trim_matches(|c: char| unicode::is_space(u32::from(c))))
        .filter(|s| !s.is_empty())
        .collect()
}

/// `frozenset(re.findall(r"IF NOT EXISTS (\w+)", text))`, sorted. The
/// schema texts are ASCII, so `\w` is `[A-Za-z0-9_]` there.
fn objects(text: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut rest = text;
    while let Some(at) = rest.find("IF NOT EXISTS ") {
        rest = &rest[at + "IF NOT EXISTS ".len()..];
        let name: String = rest
            .chars()
            .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
            .collect();
        if !name.is_empty() && !out.contains(&name) {
            out.push(name);
        }
    }
    out.sort();
    out
}

/// `re.search(rf"\b{word}\b", text)` for an ASCII word.
fn has_word(text: &str, word: &str) -> bool {
    let cps = unicode::cps(text);
    let w = unicode::cps(word);
    (0..cps.len().saturating_sub(w.len() - 1)).any(|i| {
        cps[i..].starts_with(&w)
            && (i == 0 || !unicode::is_word(cps[i - 1]))
            && cps.get(i + w.len()).is_none_or(|&c| !unicode::is_word(c))
    })
}

fn meta_value<'a>(meta: &'a [(String, String)], key: &str) -> Option<&'a str> {
    meta.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
}

fn meta_set(meta: &mut Vec<(String, String)>, key: &str, value: String) {
    match meta.iter_mut().find(|(k, _)| k == key) {
        Some(entry) => entry.1 = value,
        None => meta.push((key.to_owned(), value)),
    }
}

/// The membership tables that lack an optional column by the word search,
/// and their wanted entries.
fn lacking(master: &[(String, String)]) -> Vec<(&'static str, &'static [&'static str])> {
    OPTIONAL
        .iter()
        .filter(|(table, optional)| {
            master
                .iter()
                .find(|(n, _)| n == table)
                .is_some_and(|(_, sql)| optional.iter().any(|c| !has_word(sql, c)))
        })
        .copied()
        .collect()
}

/// `_add_membership_tables`' `wanted`: the membership and additive objects
/// `sqlite_master` lacks (by exact name), sorted; `the store id` when
/// meta's is missing or empty; `table.column` for each optional column the
/// table's text does not name as a word.
pub fn wanted(master: &[(String, String)], meta: &[(String, String)]) -> Vec<String> {
    let texts = admission::schema_texts();
    let mut names = objects(&format!("{}\n{}", texts[1], texts[2]));
    names.sort();
    let mut out: Vec<String> = names
        .into_iter()
        .filter(|o| !master.iter().any(|(n, _)| n == o))
        .collect();
    if meta_value(meta, MEMBERSHIP_STORE_ID).is_none_or(str::is_empty) {
        out.push("the store id".to_owned());
    }
    for (table, optional) in lacking(master) {
        let sql = &master.iter().find(|(n, _)| n == table).unwrap().1;
        for column in optional {
            if !has_word(sql, column) {
                out.push(format!("{table}.{column}"));
            }
        }
    }
    out
}

fn read_only(path: &str, what: &str) -> String {
    format!("store {path} is read-only; {what}")
}

/// `_add_membership_tables(conn, path, meta)`. Returns the read-only
/// warning, if any; `meta` gains the store id the table holds after.
pub fn add_membership_tables(
    conn: &Connection,
    path: &str,
    meta: &mut Vec<(String, String)>,
    provider: &mut dyn Provider,
) -> Result<Option<String>, StepError> {
    let master = master(conn)?;
    let wanted = wanted(&master, meta);
    if wanted.is_empty() {
        return Ok(None);
    }
    let minted = provider.membership_store_id().map_err(StepError::Mint)?;
    let lacking = lacking(&master);
    let texts = admission::schema_texts();
    let wrote = in_write(conn, |c| {
        for statement in statements(&texts[1])
            .into_iter()
            .chain(statements(&texts[2]))
        {
            c.execute_batch(statement)?;
        }
        for (table, optional) in &lacking {
            let mut have = Vec::new();
            {
                let mut s = c.prepare(&format!("PRAGMA table_info({table})"))?;
                let mut rows = s.query([])?;
                while let Some(row) = rows.next()? {
                    have.push(row.get_ref(1)?.as_str().unwrap_or_default().to_owned());
                }
            }
            for column in *optional {
                if !have.iter().any(|h| h == column) {
                    c.execute_batch(&format!("ALTER TABLE {table} ADD COLUMN {column} TEXT"))?;
                }
            }
        }
        c.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (MEMBERSHIP_STORE_ID, &minted),
        )?;
        Ok(())
    })?;
    if !wrote {
        return Ok(Some(read_only(
            path,
            &format!("not added: {}", wanted.join(", ")),
        )));
    }
    let failed = raised;
    let mut s = conn
        .prepare("SELECT value FROM meta WHERE key = ?")
        .map_err(failed)?;
    let mut rows = s.query([MEMBERSHIP_STORE_ID]).map_err(failed)?;
    let row = rows.next().map_err(failed)?.ok_or(StepError::NoStoreId)?;
    let value = sql_value(row.get_ref(0).map_err(failed)?)?.py_str();
    meta_set(meta, MEMBERSHIP_STORE_ID, value);
    Ok(None)
}

/// `_add_returned_marks`: the membership step, then meta's
/// [`RETURNED_BACKFILL_KEY`] set to `pending` where it is not set.
/// Returns the warnings in the order the reference logs them, beside the
/// outcome: a warning the membership step gave stays when the insert
/// after it raises, as the reference has logged it by then.
pub fn add_returned_marks(
    conn: &Connection,
    path: &str,
    meta: &mut Vec<(String, String)>,
    provider: &mut dyn Provider,
) -> (Vec<String>, Result<(), StepError>) {
    let mut warnings = Vec::new();
    let result = (|| {
        warnings.extend(add_membership_tables(conn, path, meta, provider)?);
        let wrote = in_write(conn, |c| {
            c.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, 'pending')",
                [RETURNED_BACKFILL_KEY],
            )
            .map(drop)
        })?;
        if wrote {
            meta_set(meta, RETURNED_BACKFILL_KEY, "pending".to_owned());
        } else {
            warnings.push(read_only(path, "no backfill marked"));
        }
        Ok(())
    })();
    (warnings, result)
}

/// The index steps: `text`'s objects missing from `sqlite_master` (names
/// compared lowercased), created in a write of their own.
fn add_index(conn: &Connection, path: &str, text: &str) -> Result<Option<String>, StepError> {
    let master: Vec<String> = master(conn)?
        .into_iter()
        .map(|(n, _)| n.to_lowercase())
        .collect();
    let wanted: Vec<String> = objects(text)
        .into_iter()
        .filter(|o| !master.contains(&o.to_lowercase()))
        .collect();
    if wanted.is_empty() {
        return Ok(None);
    }
    let wrote = in_write(conn, |c| {
        statements(text)
            .into_iter()
            .try_for_each(|s| c.execute_batch(s))
    })?;
    Ok((!wrote).then(|| read_only(path, &format!("not added: {}", wanted.join(", ")))))
}

/// `_add_proposals_index`: the membership step, then
/// `PROPOSALS_INDEX_SCHEMA` where it is missing. The warnings and the
/// outcome as [`add_returned_marks`] gives them.
pub fn add_proposals_index(
    conn: &Connection,
    path: &str,
    meta: &mut Vec<(String, String)>,
    provider: &mut dyn Provider,
) -> (Vec<String>, Result<(), StepError>) {
    let mut warnings = Vec::new();
    let result = (|| {
        warnings.extend(add_membership_tables(conn, path, meta, provider)?);
        warnings.extend(add_index(conn, path, &admission::schema_texts()[3])?);
        Ok(())
    })();
    (warnings, result)
}

/// `_add_session_index`: `SESSION_INDEX_SCHEMA` where it is missing.
pub fn add_session_index(conn: &Connection, path: &str) -> Result<Option<String>, StepError> {
    add_index(conn, path, &admission::schema_texts()[4])
}

/// `_add_path_targets`: nothing (step 4 adds no column).
pub fn add_path_targets(_conn: &Connection, _path: &str) {}

/// Which `KeyError` subclass [`absent_text`] is given.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Absent {
    /// `NotFoundError`: a bare key, named as one.
    NotFound,
    /// `MemoryNotFoundError`, `TombstonedError`, `NotTombstonedError`.
    Message,
}

/// `absent_text(exc)` for an exception of kind `kind` raised with `args`.
pub fn absent_text(kind: Absent, args: &[PyValue]) -> Result<PyStr, PyError> {
    let said = match args {
        [one] => pyvalue::py_str(one)?,
        _ => pyvalue::py_str(&PyValue::Tuple(args.to_vec()))?,
    };
    Ok(match kind {
        Absent::NotFound => {
            let mut out = unicode::cps("no record ");
            out.extend(said);
            out.extend(unicode::cps(" in the store"));
            out
        }
        Absent::Message => said,
    })
}

/// Whether `Path(name).name == name` on this host: [`IS_WINDOWS`].
pub const IS_WINDOWS: bool = cfg!(windows);

/// `Path(name).name == name` for a non-empty name under POSIX
/// (`windows` false) or Windows path rules.
pub fn is_plain_name(name: &[u32], windows: bool) -> bool {
    let sep = |c: u32| c == u32::from('/') || (windows && c == u32::from('\\'));
    if name.iter().any(|&c| sep(c)) || name == [u32::from('.')] {
        return false;
    }
    // `ntpath.splitdrive`: a letter-free drive is any two characters
    // ending in a colon.
    !(windows && name.len() >= 2 && name[1] == u32::from(':'))
}

/// `orphan_names(value)`: the plain names a `CREATED_AFTER_ORPHANS` value
/// holds, under the host's path rules (`windows`).
pub fn orphan_names(value: Option<&[u32]>, windows: bool) -> Vec<PyStr> {
    let Some(value) = value else {
        return Vec::new();
    };
    let Ok(loaded) = json::loads(value) else {
        return Vec::new();
    };
    let PyValue::List(names) = &loaded else {
        return Vec::new();
    };
    names
        .iter()
        .filter_map(|n| match n {
            PyValue::Str(s) if !s.is_empty() && is_plain_name(s, windows) => Some(s.clone()),
            _ => None,
        })
        .collect()
}

/// `_V8_MEMORY_NAME.match(name)` for a name that ends in `.md` (rule 3 of
/// the pattern decision): `.*-` then 26 Crockford characters then `.md`.
/// `.*` stops at a newline, so the part before the dash holds none.
fn v8_memory_name(name: &[u8]) -> bool {
    let n = name.len();
    if n < 30 || !name.ends_with(b".md") || name[n - 30] != b'-' {
        return false;
    }
    let crockford = |b: &u8| {
        b.is_ascii_digit() || (b.is_ascii_uppercase() && !matches!(b, b'I' | b'L' | b'O' | b'U'))
    };
    name[n - 29..n - 3].iter().all(crockford) && !name[..n - 30].contains(&b'\n')
}

/// Whether one child's name marks a bettermemory 8 store.
pub fn is_v8_name(name: &[u8]) -> bool {
    name == b".events.jsonl"
        || name == b".tombstones"
        || (name.starts_with(b".events.") && name.ends_with(b".jsonl"))
        || v8_memory_name(name)
}

/// `holds_v8_files(directory)`: any child's name marks a v8 store; a
/// directory that cannot be listed holds none.
pub fn holds_v8_files<F: Fs>(fs: &F, directory: &[u8]) -> bool {
    fs.listdir(directory)
        .is_ok_and(|names| names.iter().any(|n| is_v8_name(n)))
}

/// `str(PurePosixPath(path).parent)`.
#[cfg(unix)]
fn parent(pure: &str) -> String {
    match pure.rfind('/') {
        None => ".".to_owned(),
        Some(0) => "/".to_owned(),
        Some(at) if pure[..at].bytes().all(|b| b == b'/') => pure[..=at].to_owned(),
        Some(at) => pure[..at].to_owned(),
    }
}

/// `refuse_unmigrated_v8(path)`: the `UnmigratedV8DirectoryError` text when
/// no store file is at `path` and its directory holds a v8 store.
/// The reference's `Path` is the host's; this is its POSIX flavour, and
/// both the file test and the parent read the path as `PurePosixPath`
/// spells it (a trailing `/` or `/.` dropped). Not built for Windows,
/// whose `WindowsPath` takes a drive and backslashes as the parent.
#[cfg(unix)]
pub fn refuse_unmigrated_v8<F: Fs>(fs: &F, path: &str) -> Option<String> {
    use super::create::STORE_FILENAME;
    use crate::config::{pure_path, Flavour};
    use crate::fsdata::Kind;
    let is = |p: &str, kind: Kind| fs.stat(p.as_bytes()).is_ok_and(|s| s.kind == kind);
    let pure = pure_path(path, Flavour::Posix);
    let parent = parent(&pure);
    if is(&pure, Kind::Regular)
        || !is(&parent, Kind::Directory)
        || !holds_v8_files(fs, parent.as_bytes())
    {
        return None;
    }
    Some(format!(
        "{} holds a bettermemory 8 store and no {STORE_FILENAME}; run `bettermemory migrate v8` \
         to import it (`uvx bettermemory migrate v8` through the Claude Code plugin); the 8.x \
         files are left as they are",
        parent
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn words_are_bounded() {
        assert!(has_word("a unresolved_json TEXT", "unresolved_json"));
        assert!(!has_word("unresolved_jsonx", "unresolved_json"));
        assert!(!has_word("xunresolved_json", "unresolved_json"));
        assert!(has_word("a unresolved_json", "unresolved_json"));
        assert!(has_word("unresolved_json", "unresolved_json"));
    }
}
