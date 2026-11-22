//! The read side of `store_events.py`, each over a `&Connection` (the
//! reference's `self._conn`), so the writing and the read-only store both
//! serve: `iter_events`, `capture_rows`, `events_of_kinds`,
//! `session_events_of_kinds`, `events_since`, `_window_floor` and
//! `iter_events_backward`, with `log.CAPTURE_KINDS` and `NAMING_BOUND`.
//! The reference's readers are generators: a read that raises partway
//! answers the rows before the raise and the raise ([`Read`]). The rule,
//! with the reference's lines, is the README's "The event reads".

use std::collections::BTreeSet;

use rusqlite::{Connection, ToSql};

use super::rows::{event_from_columns, event_from_raw};
use super::upgrade;
use super::{log_row, sql_value, SqlValue, StoreError, LOG_SELECT};
use crate::datetime::DateTime;
use crate::json::{dumps, DumpOptions};
use crate::log::LogRow;
use crate::pyvalue::{PyStr, PyValue};
use crate::records::rows::Row;
use crate::records::RecordError;
use crate::unicode;

/// `log.CAPTURE_KINDS` (`log.py:169`), sorted.
pub const CAPTURE_KINDS: [&str; 5] = [
    "capture_close",
    "capture_member",
    "capture_open",
    "capture_relations",
    "capture_undo",
];

/// `store_events.NAMING_BOUND`: the most needles `events_of_kinds` narrows
/// by; a longer list reads every row of the kinds.
pub const NAMING_BOUND: usize = 200;

/// Why a read stopped: the exception the reference raises.
#[derive(Debug)]
pub enum EventReadError {
    Sqlite(rusqlite::Error),
    Store(StoreError),
    Record(RecordError),
    /// A Python exception by class (`OverflowError` of the cut,
    /// `UnicodeEncodeError` of a bound text holding a surrogate).
    Raised(&'static str),
    /// A naive `since` whose local offset the caller could not give.
    NotPorted(&'static str),
}

impl EventReadError {
    /// The exception's class name; a native refusal is `native:<what>`.
    pub fn class(&self) -> String {
        match self {
            EventReadError::Sqlite(e) => sqlite_class(e).to_owned(),
            EventReadError::Store(StoreError::Sqlite(e)) => sqlite_class(e).to_owned(),
            EventReadError::Store(StoreError::NotUtf8 { .. }) => "OperationalError".to_owned(),
            EventReadError::Store(_) => "native:store".to_owned(),
            EventReadError::Record(e) => e.class().to_owned(),
            EventReadError::Raised(c) => (*c).to_owned(),
            EventReadError::NotPorted(_) => "native:not_ported".to_owned(),
        }
    }
}

impl From<rusqlite::Error> for EventReadError {
    fn from(e: rusqlite::Error) -> Self {
        EventReadError::Sqlite(e)
    }
}

/// What a generator yielded before it stopped, and the raise that stopped
/// it (None when it ran out).
#[derive(Debug)]
pub struct Read<T> {
    pub items: Vec<T>,
    pub error: Option<EventReadError>,
}

impl<T> Read<T> {
    fn empty() -> Self {
        Read {
            items: Vec::new(),
            error: None,
        }
    }

    fn failed(error: EventReadError) -> Self {
        Read {
            items: Vec::new(),
            error: Some(error),
        }
    }
}

/// The `sqlite3` exception class of a failure. A statement SQLite refuses
/// to prepare (too many SQL variables, a missing index) comes back from
/// rusqlite as `SqlInputError` when it names an offset; `sqlite3` classes
/// it by the result code like any other failure.
fn sqlite_class(e: &rusqlite::Error) -> &'static str {
    match e {
        rusqlite::Error::SqlInputError { error, msg, .. } => {
            upgrade::sqlite_class(&rusqlite::Error::SqliteFailure(*error, Some(msg.clone())))
        }
        other => upgrade::sqlite_class(other),
    }
}

/// A Python str bound as a parameter: `UnicodeEncodeError` when it holds a
/// surrogate.
fn bound(text: &[u32]) -> Result<String, EventReadError> {
    unicode::to_string(text).ok_or(EventReadError::Raised("UnicodeEncodeError"))
}

/// Walks `sql` with `params`, each row through `each` (None skips it).
fn walk<T>(
    conn: &Connection,
    sql: &str,
    params: &[&dyn ToSql],
    mut each: impl FnMut(&rusqlite::Row<'_>) -> Result<Option<T>, EventReadError>,
) -> Read<T> {
    let mut out = Read::empty();
    let run = (|| -> Result<(), EventReadError> {
        let mut stmt = conn.prepare(sql)?;
        let mut rows = stmt.query(params)?;
        while let Some(row) = rows.next()? {
            if let Some(item) = each(row)? {
                out.items.push(item);
            }
        }
        Ok(())
    })();
    out.error = run.err();
    out
}

/// `Store._event_from_raw` of a fetched row: `sqlite3` decodes every
/// selected column when it fetches the row, so a text that is not UTF-8
/// in any of the seven raises.
fn raw_event(row: &rusqlite::Row<'_>) -> Result<Option<PyValue>, EventReadError> {
    let names = ["seq", "ts", "session", "kind", "payload", "prev_mac", "mac"];
    let mut columns = Vec::with_capacity(7);
    for (i, name) in names.iter().enumerate() {
        let v = sql_value(row.get_ref(i)?).map_err(EventReadError::Store)?;
        columns.push(((*name).to_owned(), v));
    }
    event_from_raw(&Row { columns }).map_err(EventReadError::Record)
}

/// `iter_events`: `log.iter_rows` after `since_seq`, the non-telemetry
/// kinds and the payloads that are not a JSON object skipped. The payload
/// is `str()` of the column, so a number is read as its digits.
pub fn iter_events(conn: &Connection, since_seq: i64) -> Read<PyValue> {
    let sql = format!("{LOG_SELECT} WHERE seq > ? ORDER BY seq");
    walk(conn, &sql, &[&since_seq], |row| {
        let r: LogRow = log_row(row).map_err(EventReadError::Store)?;
        let session = r.session.as_deref().map(unicode::cps);
        event_from_columns(
            &unicode::cps(&r.ts),
            session.as_deref(),
            &unicode::cps(&r.kind),
            &SqlValue::Text(r.payload),
        )
        .map_err(EventReadError::Record)
    })
}

/// `iter_events_backward`: the telemetry rows newest first.
pub fn iter_events_backward(conn: &Connection) -> Read<PyValue> {
    let sql = format!("{LOG_SELECT} ORDER BY seq DESC");
    walk(conn, &sql, &[], raw_event)
}

/// `capture_rows`: the rows of `kinds` that are capture kinds, after
/// `since_seq`, in log order.
pub fn capture_rows(conn: &Connection, kinds: &[PyStr], since_seq: i64) -> Read<LogRow> {
    let names: Vec<&str> = CAPTURE_KINDS
        .iter()
        .copied()
        .filter(|k| {
            let k = unicode::cps(k);
            kinds.contains(&k)
        })
        .collect();
    if names.is_empty() {
        return Read::empty();
    }
    let sql = format!(
        "{LOG_SELECT} WHERE kind IN ({}) AND seq > ? ORDER BY seq",
        vec!["?"; names.len()].join(",")
    );
    let mut params: Vec<&dyn ToSql> = names.iter().map(|n| n as &dyn ToSql).collect();
    params.push(&since_seq);
    walk(conn, &sql, &params, |row| {
        log_row(row).map(Some).map_err(EventReadError::Store)
    })
}

/// `_iso(since.astimezone(timezone.utc)).replace("+00:00", "Z")`. A naive
/// `since` is the machine's local time: `local_offset` gives its offset in
/// seconds east (the reference asks `localtime`).
pub fn window_cut(
    since: &DateTime,
    local_offset: &dyn Fn(&DateTime) -> Option<i32>,
) -> Result<String, EventReadError> {
    let aware = match since.offset_us {
        Some(_) => *since,
        None => {
            let seconds = local_offset(since).ok_or(EventReadError::NotPorted("local time"))?;
            DateTime {
                offset_us: Some(i64::from(seconds) * 1_000_000),
                ..*since
            }
        }
    };
    match aware.to_utc() {
        Some(Ok(utc)) => Ok(utc.isoformat_utc()),
        Some(Err(e)) => Err(EventReadError::Raised(e.name())),
        None => Err(EventReadError::NotPorted("naive")),
    }
}

fn prefix(cut: &str) -> String {
    cut.chars().take(19).collect()
}

/// `_window_floor`: the first seq stamped at or after `prefix` (at or
/// before `head_seq` when given), through `log_by_ts`; a store that
/// refuses the hint (OperationalError) is asked without it.
pub fn window_floor(
    conn: &Connection,
    prefix: &str,
    head_seq: Option<i64>,
) -> Result<Option<i64>, EventReadError> {
    let mut hinted = "SELECT MIN(seq) FROM log INDEXED BY log_by_ts WHERE ts >= ?".to_owned();
    let mut params: Vec<&dyn ToSql> = vec![&prefix];
    if let Some(head) = head_seq.as_ref() {
        hinted.push_str(" AND seq <= ?");
        params.push(head);
    }
    let ask = |sql: &str| -> rusqlite::Result<SqlValue> {
        conn.query_row(sql, params.as_slice(), |r| {
            sql_value(r.get_ref(0)?).map_err(|_| rusqlite::Error::InvalidQuery)
        })
    };
    let value = match ask(&hinted) {
        Err(e) if sqlite_class(&e) == "OperationalError" => {
            ask(&hinted.replace(" INDEXED BY log_by_ts", ""))?
        }
        other => other?,
    };
    Ok(match value {
        SqlValue::Integer(i) => Some(i),
        _ => None,
    })
}

/// A window read: each fetched row through `_event_from_raw`.
fn from_floor(conn: &Connection, sql: &str, params: &[&dyn ToSql]) -> Read<PyValue> {
    walk(conn, sql, params, raw_event)
}

/// `events_since`: the telemetry rows stamped at or after `since`, in log
/// order, read from the window's floor and cut on the stamp's 19-character
/// prefix.
pub fn events_since(
    conn: &Connection,
    since: &DateTime,
    local_offset: &dyn Fn(&DateTime) -> Option<i32>,
) -> Read<PyValue> {
    let cut = match window_cut(since, local_offset) {
        Ok(c) => prefix(&c),
        Err(e) => return Read::failed(e),
    };
    let floor = match window_floor(conn, &cut, None) {
        Ok(Some(f)) => f,
        Ok(None) => return Read::empty(),
        Err(e) => return Read::failed(e),
    };
    let sql = format!("{LOG_SELECT} WHERE seq >= ? AND ts >= ? ORDER BY seq");
    from_floor(conn, &sql, &[&floor, &cut])
}

/// `events_of_kinds`: the telemetry rows of `kinds` stamped at or after
/// `since`, narrowed to the payloads holding one of `naming` as the log
/// spells a string (more than [`NAMING_BOUND`] narrows nothing; an empty
/// `naming` reads nothing).
pub fn events_of_kinds(
    conn: &Connection,
    kinds: &[PyStr],
    since: &DateTime,
    naming: Option<&[PyStr]>,
    local_offset: &dyn Fn(&DateTime) -> Option<i32>,
) -> Read<PyValue> {
    let names: BTreeSet<&PyStr> = kinds.iter().collect();
    if names.is_empty() {
        return Read::empty();
    }
    let mut needles: Vec<PyStr> = Vec::new();
    if let Some(naming) = naming {
        let mut set = BTreeSet::new();
        for n in naming {
            let quoted = match dumps(&PyValue::Str(n.clone()), DumpOptions::LOG_PAYLOAD) {
                Ok(q) => q,
                Err(_) => return Read::failed(EventReadError::Raised("TypeError")),
            };
            set.insert(quoted[1..quoted.len() - 1].to_vec());
        }
        if set.is_empty() {
            return Read::empty();
        }
        if set.len() <= NAMING_BOUND {
            needles = set.into_iter().collect();
        }
    }
    let run = || -> Result<Read<PyValue>, EventReadError> {
        let cut = prefix(&window_cut(since, local_offset)?);
        let Some(floor) = window_floor(conn, &cut, None)? else {
            return Ok(Read::empty());
        };
        let names: Vec<String> = names.iter().map(|n| bound(n)).collect::<Result<_, _>>()?;
        let needles: Vec<String> = needles.iter().map(|n| bound(n)).collect::<Result<_, _>>()?;
        let mut held = String::new();
        if !needles.is_empty() {
            held = format!(
                "AND ({}) ",
                vec!["instr(payload, ?) > 0"; needles.len()].join(" OR ")
            );
        }
        let sql = format!(
            "{LOG_SELECT} WHERE kind IN ({}) AND seq >= ? AND ts >= ? {held}ORDER BY seq",
            vec!["?"; names.len()].join(",")
        );
        let mut params: Vec<&dyn ToSql> = names.iter().map(|n| n as &dyn ToSql).collect();
        params.push(&floor);
        params.push(&cut);
        params.extend(needles.iter().map(|n| n as &dyn ToSql));
        Ok(from_floor(conn, &sql, &params))
    };
    run().unwrap_or_else(Read::failed)
}

/// `session_events_of_kinds`: the telemetry rows of `kinds` recorded under
/// `session`, in log order, through `log_by_session`; a store that refuses
/// the hint (OperationalError) is read without it.
pub fn session_events_of_kinds(
    conn: &Connection,
    session: &[u32],
    kinds: &[PyStr],
) -> Read<PyValue> {
    let names: BTreeSet<&PyStr> = kinds.iter().collect();
    if names.is_empty() {
        return Read::empty();
    }
    let run = || -> Result<Read<PyValue>, EventReadError> {
        let session = bound(session)?;
        let names: Vec<String> = names.iter().map(|n| bound(n)).collect::<Result<_, _>>()?;
        let hinted = format!(
            "{} INDEXED BY log_by_session WHERE session = ? AND kind IN ({}) ORDER BY seq",
            LOG_SELECT,
            vec!["?"; names.len()].join(",")
        );
        let mut params: Vec<&dyn ToSql> = vec![&session];
        params.extend(names.iter().map(|n| n as &dyn ToSql));
        let sql = match conn.prepare(&hinted) {
            Err(e) if sqlite_class(&e) == "OperationalError" => {
                hinted.replace(" INDEXED BY log_by_session", "")
            }
            Err(e) => return Err(e.into()),
            Ok(_) => hinted,
        };
        Ok(walk(conn, &sql, &params, raw_event))
    };
    run().unwrap_or_else(Read::failed)
}
