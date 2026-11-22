//! The episode journal's reads (`store_journal.get_episode`,
//! `iter_episodes`, `count_episodes`, `episodes_by_session`,
//! `episodes_by_swarm`, `episode_session_ids`,
//! `prunable_episode_sessions` and `episode_volume`) over a connection.
//!
//! Each statement is the reference's text: the episode select
//! (`episode_select_prefix!`) with what the reference appends, so the
//! order SQLite gives is the reference's. Each row is read by
//! [`rows::episode`]. The clock is an argument (`now`); `None` reads the
//! wall clock as `utcnow` does.

use rusqlite::{Connection, ToSql};

use crate::datetime::{DateError, DateTime};
use crate::pyvalue::{PyStr, PyValue};
use crate::records::rows;
use crate::records::stored::select_rows_with;
use crate::records::{Episode, RecordError};
use crate::store::schema::{absent_text, Absent};
use crate::store::{sql_value, SqlValue, StoreError};

/// `store_schema.DEFAULT_EPISODE_TTL_DAYS`.
pub const DEFAULT_EPISODE_TTL_DAYS: i64 = 30;

/// `timedelta`'s bound on `days` (`timedelta.max.days`).
const TIMEDELTA_MAX_DAYS: i64 = 999_999_999;

/// What a journal read raises.
#[derive(Debug)]
pub enum JournalError {
    /// The query failed, or a column could not be read.
    Store(StoreError),
    /// `_row_to_episode` raised.
    Record(RecordError),
    /// `NotFoundError(episode_id)`: the id.
    NotFound(String),
    /// `TypeError`: `list.sort` met a naive and an aware `created`.
    NaiveAware,
    /// `timedelta(days=ttl)` or `now - timedelta` raised.
    Date(DateError),
    /// Declined: a session id column the reference reads with `str()`
    /// that is neither text nor an integer; the stored type.
    NotPorted(&'static str),
}

impl JournalError {
    /// The class the reference raises.
    pub fn class(&self) -> &'static str {
        match self {
            JournalError::Store(_) => "OperationalError",
            JournalError::Record(e) => e.class(),
            JournalError::NotFound(_) => "NotFoundError",
            JournalError::NaiveAware => "TypeError",
            JournalError::Date(e) => e.name(),
            JournalError::NotPorted(_) => "NotPorted",
        }
    }

    /// `absent_text(exc)` of a NotFoundError: `no record '<id>' in the store`.
    pub fn absent(&self) -> Option<PyStr> {
        match self {
            JournalError::NotFound(id) => absent_text(Absent::NotFound, &[PyValue::str(id)]).ok(),
            _ => None,
        }
    }
}

impl From<StoreError> for JournalError {
    fn from(e: StoreError) -> Self {
        JournalError::Store(e)
    }
}

impl From<rusqlite::Error> for JournalError {
    fn from(e: rusqlite::Error) -> Self {
        JournalError::Store(StoreError::from(e))
    }
}

/// `store_schema.EpisodeVolume`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EpisodeVolume {
    pub sessions: i64,
    pub episodes: i64,
    pub bytes: i64,
    pub prunable_sessions: i64,
    pub ttl_days: i64,
}

fn episodes(
    conn: &Connection,
    sql: &str,
    params: &[&dyn ToSql],
) -> Result<Vec<Episode>, JournalError> {
    select_rows_with(conn, sql, params)?
        .iter()
        .map(|row| rows::episode(row).map_err(JournalError::Record))
        .collect()
}

/// `get_episode(episode_id)`: the first row whose id matches, or
/// NotFoundError.
pub fn get_episode(conn: &Connection, episode_id: &str) -> Result<Episode, JournalError> {
    let sql = concat!(crate::episode_select_prefix!(), " WHERE id = ?");
    let mut statement = conn.prepare(sql)?;
    let names: Vec<String> = statement.column_names().map(str::to_owned).collect();
    let mut found = statement.query([episode_id])?;
    let Some(row) = found.next()? else {
        return Err(JournalError::NotFound(episode_id.to_owned()));
    };
    let mut columns = Vec::with_capacity(names.len());
    for (i, name) in names.iter().enumerate() {
        columns.push((name.clone(), sql_value(row.get_ref(i)?)?));
    }
    rows::episode(&rows::Row { columns }).map_err(JournalError::Record)
}

/// `list(iter_episodes(session_id=))`: every episode in rowid order, or
/// one session's. The reference's generator yields the rows before a bad
/// one; this answers the exception it then raises.
pub fn iter_episodes(
    conn: &Connection,
    session_id: Option<&str>,
) -> Result<Vec<Episode>, JournalError> {
    match session_id {
        None => episodes(
            conn,
            concat!(crate::episode_select_prefix!(), " ORDER BY rowid"),
            &[],
        ),
        Some(sid) => episodes(
            conn,
            concat!(
                crate::episode_select_prefix!(),
                " WHERE session_id = ? ORDER BY rowid"
            ),
            &[&sid],
        ),
    }
}

/// `count_episodes()`.
pub fn count_episodes(conn: &Connection) -> Result<i64, JournalError> {
    Ok(conn.query_row("SELECT COUNT(*) FROM episodes", [], |r| r.get(0))?)
}

/// The order `list.sort(key=created)` gives: stable, aware values by their
/// UTC instant, naive ones by their fields, TypeError for a mix of two or
/// more values.
pub fn sort_by_created(items: &mut [Episode]) -> Result<(), JournalError> {
    let aware = items
        .iter()
        .filter(|e| e.created.offset_us.is_some())
        .count();
    if items.len() >= 2 && aware != 0 && aware != items.len() {
        return Err(JournalError::NaiveAware);
    }
    items.sort_by_key(|e| {
        e.created
            .instant_us()
            .unwrap_or_else(|| e.created.wall_micros())
    });
    Ok(())
}

/// `episodes_by_session(session_id)`: the session's episodes in rowid
/// order, then sorted stably by `created` as Python orders datetimes.
pub fn episodes_by_session(
    conn: &Connection,
    session_id: &str,
) -> Result<Vec<Episode>, JournalError> {
    let mut out = iter_episodes(conn, Some(session_id))?;
    sort_by_created(&mut out)?;
    Ok(out)
}

/// `episodes_by_swarm(swarm_id)`: ordered by the `created` TEXT, then rowid.
pub fn episodes_by_swarm(conn: &Connection, swarm_id: &str) -> Result<Vec<Episode>, JournalError> {
    episodes(
        conn,
        concat!(
            crate::episode_select_prefix!(),
            " WHERE swarm_id = ? ORDER BY created, rowid"
        ),
        &[&swarm_id],
    )
}

/// `str(value)` of a session id column: text as is, an integer in
/// decimal; anything else declines as a type the reader cannot take.
fn session_text(v: SqlValue) -> Result<String, JournalError> {
    match v {
        SqlValue::Text(t) => Ok(t),
        SqlValue::Integer(i) => Ok(i.to_string()),
        other => Err(JournalError::NotPorted(match other {
            SqlValue::Null => "NULL",
            SqlValue::Real(_) => "REAL",
            _ => "BLOB",
        })),
    }
}

fn session_ids(
    conn: &Connection,
    sql: &str,
    params: &[&dyn ToSql],
) -> Result<Vec<String>, JournalError> {
    let mut statement = conn.prepare(sql)?;
    let mut found = statement.query(params)?;
    let mut out = Vec::new();
    while let Some(row) = found.next()? {
        out.push(session_text(sql_value(row.get_ref(0)?)?)?);
    }
    Ok(out)
}

/// `episode_session_ids()`: each session with an episode, by its first
/// rowid.
pub fn episode_session_ids(conn: &Connection) -> Result<Vec<String>, JournalError> {
    session_ids(
        conn,
        "SELECT session_id, MIN(rowid) AS first FROM episodes \
         GROUP BY session_id ORDER BY first",
        &[],
    )
}

/// The wall clock as `utcnow()` reads it.
fn utcnow() -> Result<DateTime, JournalError> {
    let us = match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => i64::try_from(d.as_micros()).unwrap_or(i64::MAX),
        Err(e) => -i64::try_from(e.duration().as_micros()).unwrap_or(i64::MAX),
    };
    DateTime::from_unix_micros_utc(us).map_err(JournalError::Date)
}

/// `(now - timedelta(days=ttl_days)).isoformat()`.
pub fn cutoff(ttl_days: i64, now: DateTime) -> Result<String, JournalError> {
    if ttl_days.unsigned_abs() > TIMEDELTA_MAX_DAYS as u64 {
        return Err(JournalError::Date(DateError::OverflowError));
    }
    let us = i128::from(ttl_days) * 86_400_000_000;
    Ok(now
        .minus_micros(us)
        .map_err(JournalError::Date)?
        .isoformat())
}

/// `prunable_episode_sessions(ttl_days=, now=)`: none for a TTL of zero or
/// less (the clock unread); else the sessions whose newest `created`
/// sorts as TEXT before the cutoff, by that newest text.
pub fn prunable_episode_sessions(
    conn: &Connection,
    ttl_days: i64,
    now: Option<DateTime>,
) -> Result<Vec<String>, JournalError> {
    if ttl_days <= 0 {
        return Ok(Vec::new());
    }
    let now = match now {
        Some(now) => now,
        None => utcnow()?,
    };
    let cutoff = cutoff(ttl_days, now)?;
    session_ids(
        conn,
        "SELECT session_id, MAX(created) AS newest FROM episodes \
         GROUP BY session_id HAVING newest < ? ORDER BY newest",
        &[&cutoff],
    )
}

/// `episode_volume(ttl_days=, now=)`: sessions, episodes and the UTF-8
/// bytes of every body and takeaway, then the prunable count.
pub fn episode_volume(
    conn: &Connection,
    ttl_days: i64,
    now: Option<DateTime>,
) -> Result<EpisodeVolume, JournalError> {
    let (sessions, episodes, bytes) = conn.query_row(
        "SELECT COUNT(DISTINCT session_id) AS sessions, COUNT(*) AS episodes, \
         COALESCE(SUM(LENGTH(CAST(body AS BLOB)) + \
         LENGTH(CAST(COALESCE(takeaway, '') AS BLOB))), 0) AS bytes \
         FROM episodes",
        [],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    let prunable = prunable_episode_sessions(conn, ttl_days, now)?;
    Ok(EpisodeVolume {
        sessions,
        episodes,
        bytes,
        prunable_sessions: prunable.len() as i64,
        ttl_days,
    })
}
