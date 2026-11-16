//! The episode journal's writers (`store_journal.py`: `put_episode` 44-59,
//! `delete_episode` 87-90, `write_episode` 96-122, `write_floor` 125-146)
//! and the table writers (`store_tables.py`: verifications 36-72 and
//! 111-133, conflicts 137-162, imports 165-196, quarantine 199-236), each
//! one transaction on a [`WritableStore`] returning what the reference
//! returns with what the commit wrote. README, "The store's row writers".
//!
//! Not ported: `prune_episode_sessions` (chunked, its own unit).

use rusqlite::OptionalExtension;

use super::create::Provider;
use super::home::{self, dict, not_found, record_refusal, refused, truthy, type_name, Home};
use super::open::WritableStore;
use super::tx::{Committed, Transaction};
use super::write::WriteError;
use crate::datetime::DateTime;
use crate::pyvalue::{PyStr, PyValue};
use crate::records::lax::Arg;
use crate::records::{Episode, Fields};
use crate::unicode;

/// `store_schema._CONFLICT_COLUMNS`.
pub const CONFLICT_COLUMNS: [&str; 14] = [
    "id",
    "a_id",
    "b_id",
    "summary_a",
    "summary_b",
    "similarity",
    "method",
    "detector",
    "created",
    "status",
    "verdict_ts",
    "note",
    "verdict_hash_a",
    "verdict_hash_b",
];

/// `write_episode`'s keyword arguments as the caller passes them (`None`
/// where the reference's default is None).
#[derive(Clone, Debug, PartialEq)]
pub struct EpisodeArgs {
    pub session_id: PyValue,
    pub body: PyValue,
    pub scopes: PyValue,
    pub takeaway: PyValue,
    pub swarm_id: PyValue,
    pub origin: PyValue,
}

/// `put_verification`'s arguments.
#[derive(Clone, Debug, PartialEq)]
pub struct Verification {
    pub memory_id: PyValue,
    pub verified_at: DateTime,
    pub machine_id: PyValue,
    pub verified_head: PyValue,
    pub verified_paths: Vec<PyValue>,
    pub verified_commits: Vec<PyValue>,
    pub verified_versions: Vec<PyValue>,
    pub verified_absent_paths: Vec<PyValue>,
    pub verified_path_targets: Option<Vec<(PyValue, PyValue)>>,
    pub verified_absent_path_targets: Option<Vec<(PyValue, PyValue)>>,
}

/// `str.strip()`.
fn strip(s: &[u32]) -> PyStr {
    let start = s.iter().position(|&c| !unicode::is_space(c));
    match start {
        None => Vec::new(),
        Some(a) => {
            let b = s.iter().rposition(|&c| !unicode::is_space(c)).unwrap_or(a);
            s[a..=b].to_vec()
        }
    }
}

fn no_attribute(v: &PyValue, name: &str) -> WriteError {
    refused(
        "AttributeError",
        format!("'{}' object has no attribute '{name}'", type_name(v)),
    )
}

/// `list(value or [])`.
fn listed(v: &PyValue) -> Result<PyValue, WriteError> {
    if !truthy(v) {
        return Ok(PyValue::List(Vec::new()));
    }
    match v {
        PyValue::List(items) | PyValue::Tuple(items) => Ok(PyValue::List(items.clone())),
        PyValue::Str(s) => Ok(PyValue::List(
            s.iter().map(|&c| PyValue::Str(vec![c])).collect(),
        )),
        PyValue::Dict(items) => Ok(PyValue::List(
            items.iter().map(|(k, _)| k.clone()).collect(),
        )),
        other => Err(refused(
            "TypeError",
            format!("'{}' object is not iterable", type_name(other)),
        )),
    }
}

/// `Episode(**fields)`, its refusal as `_refusal` records it.
fn episode(fields: &[(&str, PyValue)]) -> Result<Episode, WriteError> {
    let mut f = Fields::default();
    for (name, value) in fields {
        f.push(name, Arg::Py(value));
    }
    Episode::validate(&f).map_err(record_refusal)
}

/// `_require_row(tx, table, column, key)`.
fn require_row(
    tx: &Transaction<'_>,
    table: &str,
    column: &str,
    key: &str,
) -> Result<(), WriteError> {
    let found = tx
        .connection()
        .prepare(&format!("SELECT 1 FROM {table} WHERE {column} = ?"))?
        .exists([key])?;
    if found {
        Ok(())
    } else {
        Err(not_found(&PyValue::str(key)))
    }
}

/// One mutation in a transaction of its own.
fn one(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    kind: &str,
    payload: &PyValue,
    session: Option<&str>,
    require: Option<(&str, &str, &str)>,
) -> Result<Committed, WriteError> {
    let mut tx = store.begin(provider)?;
    if let Some((table, column, key)) = require {
        require_row(&tx, table, column, key)?;
    }
    tx.mutate(kind, payload, session)?;
    tx.commit()
}

impl WritableStore {
    /// `Store.put_episode(episode, session=, home=)`: one transaction;
    /// `_home_row` (which may read the wall clock once), `episode_put` with
    /// the episode's JSON dump, then `home_put` when a row was decided.
    /// Returns the episode.
    pub fn put_episode(
        &mut self,
        episode: &Episode,
        session: Option<&str>,
        home: Option<&Home>,
        provider: &mut dyn Provider,
    ) -> Result<(Episode, Committed), WriteError> {
        let id = unicode::to_string(&episode.id).ok_or(WriteError::Refused {
            class: "UnicodeEncodeError",
            text: None,
        })?;
        let mut tx = self.begin(provider)?;
        let home_row = home::home_row(&mut tx, &id, "episode", home)?;
        let payload = dict(vec![("episode", episode.dump())]);
        tx.mutate("episode_put", &payload, session)?;
        if let Some(row) = home_row {
            home::put_home(&mut tx, &row, session)?;
        }
        Ok((episode.clone(), tx.commit()?))
    }

    /// `Store.delete_episode(episode_id, session=)`: NotFoundError for an
    /// id with no row, else `episode_delete`.
    pub fn delete_episode(
        &mut self,
        episode_id: &str,
        session: Option<&str>,
        provider: &mut dyn Provider,
    ) -> Result<Committed, WriteError> {
        let payload = dict(vec![("id", PyValue::str(episode_id))]);
        one(
            self,
            provider,
            "episode_delete",
            &payload,
            session,
            Some(("episodes", "id", episode_id)),
        )
    }

    /// `Store.write_episode(...)`: a falsy or blank body refused
    /// (`ValueError`, nothing minted or read), a truthy body that is not a
    /// `str` refused as `.strip()` refuses it; then the mint once, the wall
    /// clock once unless `now` is given, the episode built and validated,
    /// and [`WritableStore::put_episode`] with the session id as session.
    pub fn write_episode(
        &mut self,
        args: &EpisodeArgs,
        now: Option<DateTime>,
        home: Option<&Home>,
        provider: &mut dyn Provider,
    ) -> Result<(Episode, Committed), WriteError> {
        let empty = || refused("ValueError", "episode body must be a non-empty string");
        if !truthy(&args.body) {
            return Err(empty());
        }
        let PyValue::Str(body) = &args.body else {
            return Err(no_attribute(&args.body, "strip"));
        };
        let body = strip(body);
        if body.is_empty() {
            return Err(empty());
        }
        let id = provider.ulid().map_err(WriteError::Clock)?;
        let created = match now {
            Some(t) => t,
            None => provider.wall_now().map_err(WriteError::Clock)?,
        };
        let mut text = body;
        text.push(u32::from('\n'));
        let scopes = listed(&args.scopes)?;
        let takeaway = if truthy(&args.takeaway) {
            match &args.takeaway {
                PyValue::Str(t) => PyValue::Str(strip(t)),
                other => return Err(no_attribute(other, "strip")),
            }
        } else {
            PyValue::None
        };
        let built = episode(&[
            ("id", PyValue::str(&id)),
            ("session_id", args.session_id.clone()),
            ("created", PyValue::DateTime(created)),
            ("body", PyValue::Str(text)),
            ("scopes", scopes),
            ("takeaway", takeaway),
            ("swarm_id", args.swarm_id.clone()),
            ("origin", args.origin.clone()),
        ])?;
        let session = unicode::to_string(&built.session_id);
        self.put_episode(&built, session.as_deref(), home, provider)
    }

    /// `Store.write_floor(session_id=, origin=, now=, home=)`: the mint
    /// once, the wall clock once unless `now` is given, the floor episode,
    /// [`WritableStore::put_episode`].
    pub fn write_floor(
        &mut self,
        session_id: &PyValue,
        origin: &PyValue,
        now: Option<DateTime>,
        home: Option<&Home>,
        provider: &mut dyn Provider,
    ) -> Result<(Episode, Committed), WriteError> {
        let id = provider.ulid().map_err(WriteError::Clock)?;
        let created = match now {
            Some(t) => t,
            None => provider.wall_now().map_err(WriteError::Clock)?,
        };
        let built = episode(&[
            ("id", PyValue::str(&id)),
            ("session_id", session_id.clone()),
            ("created", PyValue::DateTime(created)),
            (
                "body",
                PyValue::str("(session-tag floor, no takeaway recorded)\n"),
            ),
            ("scopes", PyValue::List(Vec::new())),
            ("takeaway", PyValue::None),
            ("origin", origin.clone()),
            ("is_floor", PyValue::Bool(true)),
        ])?;
        let session = unicode::to_string(&built.session_id);
        self.put_episode(&built, session.as_deref(), home, provider)
    }

    /// `Store.put_verification(...)`: `verification_put` with the stamp
    /// (`verified_at` as `datetime.isoformat()` writes it; each target map
    /// only when given).
    pub fn put_verification(
        &mut self,
        v: &Verification,
        session: Option<&str>,
        provider: &mut dyn Provider,
    ) -> Result<Committed, WriteError> {
        let mut items = vec![
            ("memory_id", v.memory_id.clone()),
            ("machine_id", v.machine_id.clone()),
            ("verified_at", PyValue::str(&v.verified_at.isoformat())),
            ("verified_head", v.verified_head.clone()),
            ("verified_paths", PyValue::List(v.verified_paths.clone())),
            (
                "verified_commits",
                PyValue::List(v.verified_commits.clone()),
            ),
            (
                "verified_versions",
                PyValue::List(v.verified_versions.clone()),
            ),
            (
                "verified_absent_paths",
                PyValue::List(v.verified_absent_paths.clone()),
            ),
        ];
        for (field, targets) in [
            ("verified_path_targets", &v.verified_path_targets),
            (
                "verified_absent_path_targets",
                &v.verified_absent_path_targets,
            ),
        ] {
            if let Some(t) = targets {
                // `dict(targets)`: a later key wins, in its first place.
                let mut out: Vec<(PyValue, PyValue)> = Vec::new();
                for (k, val) in t {
                    match out.iter_mut().find(|(ok, _)| ok == k) {
                        Some(slot) => slot.1 = val.clone(),
                        None => out.push((k.clone(), val.clone())),
                    }
                }
                items.push((field, PyValue::Dict(out)));
            }
        }
        one(
            self,
            provider,
            "verification_put",
            &dict(items),
            session,
            None,
        )
    }

    /// `Store.delete_verification(memory_id, machine_id=, session=)`:
    /// NotFoundError (`(memory_id, machine_id)`) when no row matches, a
    /// None machine matching a NULL or empty one.
    pub fn delete_verification(
        &mut self,
        memory_id: &str,
        machine_id: Option<&str>,
        session: Option<&str>,
        provider: &mut dyn Provider,
    ) -> Result<Committed, WriteError> {
        let machine = machine_id.map_or(PyValue::None, PyValue::str);
        let mut tx = self.begin(provider)?;
        let found = tx
            .connection()
            .query_row(
                "SELECT 1 FROM verifications WHERE memory_id = ? \
                 AND COALESCE(machine_id, '') = COALESCE(?, '')",
                rusqlite::params![memory_id, machine_id],
                |_| Ok(()),
            )
            .optional()?;
        if found.is_none() {
            return Err(not_found(&PyValue::Tuple(vec![
                PyValue::str(memory_id),
                machine.clone(),
            ])));
        }
        let payload = dict(vec![
            ("memory_id", PyValue::str(memory_id)),
            ("machine_id", machine),
        ]);
        tx.mutate("verification_delete", &payload, session)?;
        tx.commit()
    }

    /// `Store.put_conflict(record, session=)`: the record's columns
    /// (`record.get`, None when absent); a falsy id, a_id or b_id refused
    /// before the transaction.
    pub fn put_conflict(
        &mut self,
        record: &PyValue,
        session: Option<&str>,
        provider: &mut dyn Provider,
    ) -> Result<Committed, WriteError> {
        let PyValue::Dict(items) = record else {
            return Err(no_attribute(record, "get"));
        };
        let get = |c: &str| {
            let key = PyValue::str(c);
            items
                .iter()
                .find(|(k, _)| *k == key)
                .map_or(PyValue::None, |(_, v)| v.clone())
        };
        let conflict: Vec<(&str, PyValue)> =
            CONFLICT_COLUMNS.iter().map(|c| (*c, get(c))).collect();
        for (required, value) in conflict.iter().take(3) {
            if !truthy(value) {
                return Err(refused(
                    "ValueError",
                    format!("a conflict needs {required}"),
                ));
            }
        }
        let payload = dict(vec![("conflict", dict(conflict))]);
        one(self, provider, "conflict_put", &payload, session, None)
    }

    /// `Store.delete_conflict(conflict_id, session=)`.
    pub fn delete_conflict(
        &mut self,
        conflict_id: &str,
        session: Option<&str>,
        provider: &mut dyn Provider,
    ) -> Result<Committed, WriteError> {
        let payload = dict(vec![("id", PyValue::str(conflict_id))]);
        one(
            self,
            provider,
            "conflict_delete",
            &payload,
            session,
            Some(("conflicts", "id", conflict_id)),
        )
    }

    /// `Store.put_import(source, content_hash=, imported_at=, memory_id=)`.
    pub fn put_import(
        &mut self,
        source: &PyValue,
        content_hash: &PyValue,
        imported_at: &PyValue,
        memory_id: &PyValue,
        session: Option<&str>,
        provider: &mut dyn Provider,
    ) -> Result<Committed, WriteError> {
        let payload = dict(vec![
            ("source", source.clone()),
            ("content_hash", content_hash.clone()),
            ("imported_at", imported_at.clone()),
            ("memory_id", memory_id.clone()),
        ]);
        one(self, provider, "import_put", &payload, session, None)
    }

    /// `Store.delete_import(source, session=)`.
    pub fn delete_import(
        &mut self,
        source: &str,
        session: Option<&str>,
        provider: &mut dyn Provider,
    ) -> Result<Committed, WriteError> {
        let payload = dict(vec![("source", PyValue::str(source))]);
        one(
            self,
            provider,
            "import_delete",
            &payload,
            session,
            Some(("imports", "source", source)),
        )
    }

    /// `Store.put_quarantine(name, reason=, at=, detail=, remote=, size=,
    /// sha256=)`: `fields` in that order, the payload in the reference's
    /// (name, reason, detail, remote, at, size, sha256).
    pub fn put_quarantine(
        &mut self,
        fields: &Quarantine,
        session: Option<&str>,
        provider: &mut dyn Provider,
    ) -> Result<Committed, WriteError> {
        let payload = dict(vec![
            ("name", fields.name.clone()),
            ("reason", fields.reason.clone()),
            ("detail", fields.detail.clone()),
            ("remote", fields.remote.clone()),
            ("at", fields.at.clone()),
            ("size", fields.size.clone()),
            ("sha256", fields.sha256.clone()),
        ]);
        one(self, provider, "quarantine_put", &payload, session, None)
    }

    /// `Store.delete_quarantine(name, session=)`.
    pub fn delete_quarantine(
        &mut self,
        name: &str,
        session: Option<&str>,
        provider: &mut dyn Provider,
    ) -> Result<Committed, WriteError> {
        let payload = dict(vec![("name", PyValue::str(name))]);
        one(
            self,
            provider,
            "quarantine_delete",
            &payload,
            session,
            Some(("quarantine", "name", name)),
        )
    }
}

/// `put_quarantine`'s arguments.
#[derive(Clone, Debug, PartialEq)]
pub struct Quarantine {
    pub name: PyValue,
    pub reason: PyValue,
    pub at: PyValue,
    pub detail: PyValue,
    pub remote: PyValue,
    pub size: PyValue,
    pub sha256: PyValue,
}
