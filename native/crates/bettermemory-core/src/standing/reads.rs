//! The store-reading half of `standing.py` (unit standing-2): the body
//! sha memo (`_SHA_BOUND`, `_SHAS`, `_clear_shas`), `_current`,
//! `answers`, `Backlog`, `backlog`, `guarded_ids`, `makes_standing`,
//! `held_ids`, `standing_ids`, `refusal_rows` and `held_by_removal`.
//!
//! The store is the trait [`StandingStore`] (its reads named as the
//! reference's `Store` methods) and the admission the trait [`Admission`]
//! (`admission.RecordView` with `admits_row`). Whether the write review is
//! in force (`review.current_tau() is not None`) is the argument
//! `tau_set`, as [`super::asking`] takes it.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{LazyLock, Mutex, MutexGuard};

use sha2::{Digest, Sha256};

use super::{is_standing, record_text, TAU_STANDING};
use crate::admission::EveryRecord;
use crate::datetime::DateError;
use crate::in_force::{in_force, LinkReader, RecordView};
use crate::pyvalue::{PyStr, PyValue};
use crate::records::{LinkType, Memory, MemoryLink};
use crate::store::caller::ModelError;
use crate::unicode;

/// `_SHA_BOUND`: the entries the body sha memo keeps.
pub const SHA_BOUND: usize = 65_536;

/// `models.Category.AMBIENT.value`.
const AMBIENT: &str = "ambient";

/// One row of `Store.standing_rows`, its keys as fields.
#[derive(Clone, Debug, PartialEq)]
pub struct StandingRow {
    pub id: String,
    pub category: Option<String>,
    pub updated: String,
    pub scopes: Vec<PyStr>,
    pub recorded: Option<PyStr>,
    pub log_mac: Option<String>,
    pub probability: Option<f64>,
    pub body_sha256: Option<String>,
}

/// What the reference raises out of these reads.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReadError {
    /// `store.NotFoundError(id)`.
    NotFound(String),
    /// `UnicodeEncodeError`: a body holding a lone surrogate, which
    /// `body.encode("utf-8")` refuses.
    Unencodable,
    /// What `admits_row` raised.
    Model(ModelError),
    /// `updated.astimezone(timezone.utc)` raised (OverflowError).
    Date(DateError),
    /// A naive ``updated``: the reference reads it as the machine's local
    /// time, which is not ported.
    NaiveUpdated,
}

/// The store's reads this half makes.
pub trait StandingStore: LinkReader {
    /// `Store.body_of(id)`: the active record's body, or None.
    fn body_of(&mut self, id: &str) -> Option<PyStr>;
    /// `Store.standing_rows(ids)`: every active record (or those of
    /// `ids`), newest ``updated`` first.
    fn standing_rows(&mut self, ids: Option<&[String]>) -> Vec<StandingRow>;
    /// `Store.digest_unanswered_ids()`.
    fn digest_unanswered_ids(&mut self) -> Option<Vec<String>>;
    /// `Store.returned_rows(ids)`: the marked ids, in the order of the
    /// reference's dict (its values are not read here).
    fn returned_rows(&mut self, ids: Option<&[String]>) -> Vec<String>;
    /// `Store.standing_candidate_ids(tau)`.
    fn standing_candidate_ids(&mut self, tau: Option<f64>) -> Option<Vec<String>>;
    /// `Store.get_many(ids)`: the records found, in the order asked.
    fn get_many(&mut self, ids: &[String]) -> Vec<Memory>;
    /// `Store.tombstone_links(id)`: `ReadError::NotFound` for no
    /// tombstone.
    fn tombstone_links(&mut self, id: &str) -> Result<Vec<MemoryLink>, ReadError>;
}

/// `admission.RecordView` with `admits_row`, which `backlog`,
/// `standing_ids` and `refusal_rows` read.
pub trait Admission: RecordView {
    fn admits_row(
        &mut self,
        scopes: &[PyStr],
        recorded: Option<&[u32]>,
    ) -> Result<bool, ModelError>;
}

impl Admission for EveryRecord {
    fn admits_row(
        &mut self,
        scopes: &[PyStr],
        recorded: Option<&[u32]>,
    ) -> Result<bool, ModelError> {
        EveryRecord::admits_row(self, scopes, recorded)
    }
}

type Key = (String, String);

/// `_SHAS`: an ordered dict as an LRU (a tick per entry, oldest first).
struct ShaMemo {
    bound: usize,
    tick: u64,
    map: HashMap<Key, (String, u64)>,
    order: BTreeMap<u64, Key>,
}

impl ShaMemo {
    fn get(&mut self, key: &Key) -> Option<String> {
        let (sha, old) = self.map.get(key)?.clone();
        self.order.remove(&old);
        self.tick += 1;
        self.order.insert(self.tick, key.clone());
        self.map.insert(key.clone(), (sha.clone(), self.tick));
        Some(sha)
    }

    /// `_SHAS[key] = sha`, then the oldest popped past the bound. An
    /// existing key keeps its place, as a dict assignment does.
    fn put(&mut self, key: Key, sha: String) {
        if let Some(entry) = self.map.get_mut(&key) {
            entry.0 = sha;
        } else {
            self.tick += 1;
            self.order.insert(self.tick, key.clone());
            self.map.insert(key, (sha, self.tick));
        }
        while self.map.len() > self.bound {
            let Some((_, oldest)) = self.order.pop_first() else {
                break;
            };
            self.map.remove(&oldest);
        }
    }
}

static SHAS: LazyLock<Mutex<ShaMemo>> = LazyLock::new(|| {
    Mutex::new(ShaMemo {
        bound: SHA_BOUND,
        tick: 0,
        map: HashMap::new(),
        order: BTreeMap::new(),
    })
});

fn shas() -> MutexGuard<'static, ShaMemo> {
    SHAS.lock().unwrap_or_else(|e| e.into_inner())
}

/// `_clear_shas()`.
pub fn clear_shas() {
    let mut memo = shas();
    memo.map.clear();
    memo.order.clear();
}

/// Rebinds `_SHA_BOUND`, as a caller of the reference may rebind the
/// module global; the next insert trims to it.
pub fn set_sha_bound(bound: usize) {
    shas().bound = bound;
}

/// `len(_SHAS)`.
pub fn sha_count() -> usize {
    shas().map.len()
}

/// `store_rows.body_sha256(body)`: the hex sha256 of the UTF-8 body.
pub fn body_sha256(body: &[u32]) -> Result<String, ReadError> {
    let text = unicode::to_string(body).ok_or(ReadError::Unencodable)?;
    Ok(Sha256::digest(text.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

/// `_current(store, row)`: the row's answer if it was given for the
/// record's current body.
pub fn current<S: StandingStore + ?Sized>(
    store: &mut S,
    row: &StandingRow,
) -> Result<Option<f64>, ReadError> {
    let Some(probability) = row.probability else {
        return Ok(None);
    };
    let key = row
        .log_mac
        .as_ref()
        .map(|mac| (row.id.clone(), mac.clone()));
    let mut sha = key.as_ref().and_then(|k| shas().get(k));
    if sha.is_none() {
        let Some(body) = store.body_of(&row.id) else {
            return Ok(None);
        };
        let fresh = body_sha256(&body)?;
        if let Some(k) = key {
            shas().put(k, fresh.clone());
        }
        sha = Some(fresh);
    }
    Ok((sha == row.body_sha256).then_some(probability))
}

fn cps_of(text: Option<&String>) -> Option<PyStr> {
    text.map(|t| unicode::cps(t))
}

fn text_of(cps: &[u32]) -> String {
    cps.iter()
        .map(|&c| char::from_u32(c).unwrap_or('\u{FFFD}'))
        .collect()
}

/// `dict.fromkeys(items)`: first occurrences, in order.
fn first_seen(items: &[String]) -> Vec<String> {
    let mut seen = HashSet::new();
    items
        .iter()
        .filter(|i| seen.insert(i.as_str()))
        .cloned()
        .collect()
}

/// `answers(store, ids)`: per active record of `ids`, its current answer,
/// in the order of the rows (a dict: a repeated id keeps its first place
/// and its last value).
pub fn answers<S: StandingStore + ?Sized>(
    store: &mut S,
    ids: &[String],
) -> Result<Vec<(String, Option<f64>)>, ReadError> {
    let mut out: Vec<(String, Option<f64>)> = Vec::new();
    let mut place: HashMap<String, usize> = HashMap::new();
    for row in store.standing_rows(Some(ids)) {
        let value = current(store, &row)?;
        match place.get(&row.id) {
            Some(&at) => out[at].1 = value,
            None => {
                place.insert(row.id.clone(), out.len());
                out.push((row.id, value));
            }
        }
    }
    Ok(out)
}

/// `Backlog`: the admitted records with no answer for their current body.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Backlog {
    pub ids: Vec<String>,
}

impl Backlog {
    /// `Backlog.count`.
    pub fn count(&self) -> usize {
        self.ids.len()
    }
}

/// `backlog(store, admission)`.
pub fn backlog<S: StandingStore + ?Sized, A: Admission + ?Sized>(
    store: &mut S,
    admission: &mut A,
) -> Result<Backlog, ReadError> {
    let asked = store.digest_unanswered_ids();
    let mut waiting = Vec::new();
    for row in store.standing_rows(asked.as_deref()) {
        if !admission
            .admits_row(&row.scopes, row.recorded.as_deref())
            .map_err(ReadError::Model)?
        {
            continue;
        }
        if current(store, &row)?.is_none() {
            waiting.push(row.id);
        }
    }
    Ok(Backlog { ids: waiting })
}

/// `guarded_ids(store, ids)`: `standing_ids` under every record, held
/// ones included.
pub fn guarded_ids<S: StandingStore>(
    store: &mut S,
    ids: &[String],
) -> Result<Vec<String>, ReadError> {
    if ids.is_empty() {
        return Ok(Vec::new());
    }
    standing_ids(store, &mut EveryRecord, Some(ids), true)
}

/// `makes_standing(store, record_id, category=, probability=,
/// body_changed=)`: the checks in the reference's order.
pub fn makes_standing<S: StandingStore>(
    store: &mut S,
    record_id: &str,
    category: Option<&str>,
    probability: Option<f64>,
    body_changed: bool,
) -> Result<bool, ReadError> {
    if !is_standing(
        cps_of(category.map(str::to_owned).as_ref()).as_deref(),
        probability,
    ) {
        return Ok(false);
    }
    let asked = [record_id.to_owned()];
    let forced = in_force(store, &asked, &mut EveryRecord);
    let forced = forced
        .into_iter()
        .find(|f| f.asked == record_id)
        .expect("in_force answers every asked id");
    if forced.history() {
        return Ok(false);
    }
    if forced.held {
        return Ok(true);
    }
    if !body_changed {
        let rows = store.standing_rows(Some(&asked));
        if let Some(first) = rows.first() {
            let answer = answers(store, &asked)?
                .into_iter()
                .find(|(id, _)| id == record_id)
                .and_then(|(_, p)| p);
            if is_standing(cps_of(first.category.as_ref()).as_deref(), answer) {
                return Ok(false);
            }
        }
    }
    Ok(true)
}

/// `held_ids(store, ids)`, given whether the review is in force.
pub fn held_ids<S: StandingStore + ?Sized>(
    store: &mut S,
    ids: Option<&[String]>,
    tau_set: bool,
) -> Result<HashSet<String>, ReadError> {
    if !super::asking(tau_set) {
        return Ok(HashSet::new());
    }
    let marked = store.returned_rows(ids);
    if marked.is_empty() {
        return Ok(HashSet::new());
    }
    let mut out = HashSet::new();
    for row in store.standing_rows(Some(&marked)) {
        if is_standing(
            cps_of(row.category.as_ref()).as_deref(),
            current(store, &row)?,
        ) {
            out.insert(row.id);
        }
    }
    Ok(out)
}

/// `standing_ids(store, admission, ids, held=)`: the admitted standing
/// records in force, in the rows' order.
pub fn standing_ids<S: StandingStore, A: Admission>(
    store: &mut S,
    admission: &mut A,
    ids: Option<&[String]>,
    held: bool,
) -> Result<Vec<String>, ReadError> {
    let ids: Option<Vec<String>> = match ids {
        Some([]) => return Ok(Vec::new()),
        Some(given) => Some(given.to_vec()),
        None => match store.standing_candidate_ids(TAU_STANDING) {
            Some(c) if c.is_empty() => return Ok(Vec::new()),
            other => other,
        },
    };
    let mut candidates: Vec<String> = Vec::new();
    for row in store.standing_rows(ids.as_deref()) {
        if row.category.as_deref() != Some(AMBIENT)
            && (TAU_STANDING.is_none() || row.probability.is_none())
        {
            continue;
        }
        if !admission
            .admits_row(&row.scopes, row.recorded.as_deref())
            .map_err(ReadError::Model)?
        {
            continue;
        }
        if is_standing(
            cps_of(row.category.as_ref()).as_deref(),
            current(store, &row)?,
        ) {
            candidates.push(row.id);
        }
    }
    if candidates.is_empty() {
        return Ok(Vec::new());
    }
    let resolved: HashMap<String, (bool, bool)> = in_force(store, &candidates, admission)
        .into_iter()
        .map(|f| (f.asked.clone(), (f.history(), f.held)))
        .collect();
    Ok(candidates
        .into_iter()
        .filter(|rid| {
            let (history, is_held) = resolved[rid];
            !history && (held || !is_held)
        })
        .collect())
}

/// `refusal_rows(store, ids, admission)`: per record found, a dict with
/// the reference's keys in its order.
pub fn refusal_rows<S: StandingStore + ?Sized, A: Admission + ?Sized>(
    store: &mut S,
    ids: &[String],
    admission: &mut A,
) -> Result<Vec<PyValue>, ReadError> {
    admission.prime(ids);
    let mut rows = Vec::new();
    for memory in store.get_many(&first_seen(ids)) {
        let fields = &memory.fields;
        let admitted = admission.admits_id(&text_of(&fields.id));
        let utc = match fields.updated.to_utc() {
            None => return Err(ReadError::NaiveUpdated),
            Some(r) => r.map_err(ReadError::Date)?,
        };
        let date = format!("{:04}-{:02}-{:02}", utc.year, utc.month, utc.day);
        let mut row = vec![
            (PyValue::str("id"), PyValue::Str(fields.id.clone())),
            (
                PyValue::str("scopes"),
                PyValue::List(fields.scopes.iter().cloned().map(PyValue::Str).collect()),
            ),
            (PyValue::str("updated"), PyValue::str(&date)),
            (
                PyValue::str("text"),
                if admitted {
                    PyValue::Str(record_text(&fields.body))
                } else {
                    PyValue::None
                },
            ),
        ];
        if !admitted {
            row.push((PyValue::str("admitted"), PyValue::Bool(false)));
        }
        rows.push(PyValue::Dict(row));
    }
    Ok(rows)
}

/// `held_by_removal(store, removed)`: each held ``supersedes`` target of
/// a removed record's tombstone, first occurrence first.
pub fn held_by_removal<S: StandingStore + ?Sized>(
    store: &mut S,
    removed: &[String],
    tau_set: bool,
) -> Result<Vec<String>, ReadError> {
    let mut targets: Vec<String> = Vec::new();
    for record_id in removed {
        let links = match store.tombstone_links(record_id) {
            Err(ReadError::NotFound(_)) => continue,
            other => other?,
        };
        targets.extend(
            links
                .iter()
                .filter(|l| l.link_type == LinkType::Supersedes)
                .map(|l| text_of(&l.target_id)),
        );
    }
    if targets.is_empty() {
        return Ok(Vec::new());
    }
    let held = held_ids(store, Some(&targets), tau_set)?;
    Ok(first_seen(&targets)
        .into_iter()
        .filter(|i| held.contains(i))
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn memo(bound: usize) -> ShaMemo {
        ShaMemo {
            bound,
            tick: 0,
            map: HashMap::new(),
            order: BTreeMap::new(),
        }
    }

    fn key(id: &str) -> Key {
        (id.to_owned(), "m".to_owned())
    }

    #[test]
    fn an_insert_of_a_present_key_keeps_its_place() {
        let mut m = memo(2);
        m.put(key("a"), "1".to_owned());
        m.put(key("b"), "2".to_owned());
        // A dict assignment to a present key neither moves it nor trims.
        m.put(key("a"), "3".to_owned());
        m.put(key("c"), "4".to_owned());
        assert!(!m.map.contains_key(&key("a")), "a stayed the oldest");
        assert_eq!(m.get(&key("b")), Some("2".to_owned()));
        assert_eq!(m.get(&key("c")), Some("4".to_owned()));
        let mut m = memo(2);
        m.put(key("a"), "1".to_owned());
        m.put(key("a"), "3".to_owned());
        assert_eq!(m.get(&key("a")), Some("3".to_owned()));
        assert_eq!(m.map.len(), 1);
    }
}
