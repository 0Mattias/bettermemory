//! The store's record tables read into validated records: each row
//! selected as the reference selects it (`store_records._MEMORY_SELECT`,
//! `_TOMBSTONE_SELECT`, `store_journal._EPISODE_SELECT`: the row and the
//! payload of the log row its `log_mac` names, as `attestation_payload`),
//! in rowid order, then read by [`rows`]. The raw column readers
//! (`StoreFile::each_memory` and kin) stay as they are: the fold needs
//! both.

use rusqlite::{Connection, ToSql};

use super::rows::{self, Row};
use super::{Episode, Memory, RecordError, Tombstone};
use crate::store::{sql_value, StoreError, StoreFile};

/// `store_records._MEMORY_SELECT`, in rowid order.
pub const MEMORY_SELECT: &str =
    "SELECT memories.*, (SELECT payload FROM log WHERE mac = memories.log_mac \
     AND kind = 'memory_put' LIMIT 1) AS attestation_payload FROM memories ORDER BY memories.rowid";

/// `store_records._TOMBSTONE_SELECT`, in rowid order.
pub const TOMBSTONE_SELECT: &str = "SELECT tombstones.*, (SELECT payload FROM log WHERE mac = tombstones.log_mac \
     AND kind = 'tombstone_put' LIMIT 1) AS attestation_payload FROM tombstones ORDER BY tombstones.rowid";

/// `store_journal._EPISODE_SELECT` as the reference spells it, with no
/// order: the readers of `store::journal` append to it what the
/// reference appends.
#[macro_export]
macro_rules! episode_select_prefix {
    () => {
        "SELECT episodes.*, (SELECT payload FROM log WHERE mac = episodes.log_mac \
         AND kind = 'episode_put' LIMIT 1) AS attestation_payload FROM episodes"
    };
}

/// `store_journal._EPISODE_SELECT`, in rowid order.
pub const EPISODE_SELECT: &str = concat!(episode_select_prefix!(), " ORDER BY episodes.rowid");

/// Every row `select` returns, each as a [`Row`] of its named columns.
/// A text value that is not UTF-8 fails the read, as `sqlite3` fails it.
pub fn select_rows(conn: &Connection, select: &str) -> Result<Vec<Row>, StoreError> {
    select_rows_with(conn, select, &[])
}

/// [`select_rows`] of a statement with parameters bound in order.
pub fn select_rows_with(
    conn: &Connection,
    select: &str,
    params: &[&dyn ToSql],
) -> Result<Vec<Row>, StoreError> {
    let mut statement = conn.prepare(select)?;
    let names: Vec<String> = statement.column_names().map(str::to_owned).collect();
    let mut rows = statement.query(params)?;
    let mut out = Vec::new();
    while let Some(row) = rows.next()? {
        let mut columns = Vec::with_capacity(names.len());
        for (i, name) in names.iter().enumerate() {
            columns.push((name.clone(), sql_value(row.get_ref(i)?)?));
        }
        out.push(Row { columns });
    }
    Ok(out)
}

/// Each row and the record read from it, or the exception it raises.
pub type Read<T> = Vec<(Row, Result<T, RecordError>)>;

impl StoreFile {
    /// `iter_memories` with the model: each memory row and the record the
    /// reference reads from it, or the exception it raises.
    pub fn memory_records(&self) -> Result<Read<Memory>, StoreError> {
        Ok(select_rows(self.connection(), MEMORY_SELECT)?
            .into_iter()
            .map(|row| {
                let record = rows::memory(&row);
                (row, record)
            })
            .collect())
    }

    /// The tombstone rows and their records.
    pub fn tombstone_records(&self) -> Result<Read<Tombstone>, StoreError> {
        Ok(select_rows(self.connection(), TOMBSTONE_SELECT)?
            .into_iter()
            .map(|row| {
                let record = rows::tombstone(&row);
                (row, record)
            })
            .collect())
    }

    /// The episode rows and their records.
    pub fn episode_records(&self) -> Result<Read<Episode>, StoreError> {
        Ok(select_rows(self.connection(), EPISODE_SELECT)?
            .into_iter()
            .map(|row| {
                let record = rows::episode(&row);
                (row, record)
            })
            .collect())
    }
}
