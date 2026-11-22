//! The document frequency memo of `store_index.py`: the process-wide memo
//! keyed by store (`_DOC_FREQS`, `DOC_FREQ_STORES`, `DOC_FREQ_TERMS`), a
//! store's admission inputs grouped by equal inputs (`_DocFreqInputs`, the
//! grouping of `Store._read_doc_freq_inputs`, `_row_mask`), the masks of a
//! term brought up to date for the rows changed since they were read
//! (`_patched_masks`), the mask of the rows a caller is admitted to
//! (`_admitted_mask`), and the call that puts them together
//! (`_memoised_document_frequencies`, `_current_inputs`,
//! `_keep_doc_freqs`).
//!
//! A row is a bit: its position in the scan. A mask is a bit set of `u64`
//! words, as the reference's Python `int`, of any width. The store's side
//! (its memory head, its rows, the FTS table's hits, the log's rows that
//! advance an entry) is the trait [`DocFreqSource`].

use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};

use crate::json;
use crate::pyvalue::{PyError, PyValue};

/// Stores the memo keeps, the most recently read.
pub const DOC_FREQ_STORES: usize = 8;
/// Terms the memo keeps per store, the most recently used.
pub const DOC_FREQ_TERMS: usize = 4096;
/// Changed rows a term's masks are patched for; past it they are read whole.
pub const PATCH_ROWS: usize = 8;
/// Rows of a term the patch reads before it gives way to the whole read.
pub const PATCH_WINDOW: usize = 64;

/// What fails a memoised call: the reference's exception, by class.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DocFreqError {
    /// `json.loads` of a row's scopes text raised (`_load_json`).
    Py(PyError),
    /// `_row_mask` wrote past its bytearray (a position at or past the
    /// width rounded up to bytes).
    IndexError,
    /// The store's side failed (an SQLite error the reference raises).
    Source(String),
}

/// A row bit set, as the reference's Python `int` masks.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Mask(Vec<u64>);

impl Mask {
    pub fn set(&mut self, position: usize) {
        let word = position / 64;
        if word >= self.0.len() {
            self.0.resize(word + 1, 0);
        }
        self.0[word] |= 1 << (position % 64);
    }

    pub fn clear(&mut self, position: usize) {
        if let Some(word) = self.0.get_mut(position / 64) {
            *word &= !(1u64 << (position % 64));
        }
    }

    pub fn get(&self, position: usize) -> bool {
        self.0
            .get(position / 64)
            .is_some_and(|w| w >> (position % 64) & 1 == 1)
    }

    pub fn or_with(&mut self, other: &Mask) {
        if other.0.len() > self.0.len() {
            self.0.resize(other.0.len(), 0);
        }
        for (a, b) in self.0.iter_mut().zip(&other.0) {
            *a |= b;
        }
    }

    /// `(self & other).bit_count()`.
    pub fn and_count(&self, other: &Mask) -> usize {
        self.0
            .iter()
            .zip(&other.0)
            .map(|(a, b)| (a & b).count_ones() as usize)
            .sum()
    }

    pub fn count(&self) -> usize {
        self.0.iter().map(|w| w.count_ones() as usize).sum()
    }

    pub fn is_zero(&self) -> bool {
        self.0.iter().all(|w| *w == 0)
    }
}

/// `_row_mask`: the mask with bit `p` set for each position, built in a
/// bytearray of `(width + 7) // 8` bytes; a position past it raises
/// IndexError, as the bytearray does.
pub fn row_mask(positions: &[usize], width: usize) -> Result<Mask, DocFreqError> {
    // In u128, so no width overflows (the reference's int does not).
    let limit = (width as u128).div_ceil(8) * 8;
    let mut mask = Mask::default();
    for &position in positions {
        if position as u128 >= limit {
            return Err(DocFreqError::IndexError);
        }
        mask.set(position);
    }
    Ok(mask)
}

/// `Actor(client=client, model=model)`, made where either is not None.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DocActor {
    pub client: Option<String>,
    pub model: Option<String>,
}

impl DocActor {
    pub fn of(client: &Option<String>, model: &Option<String>) -> Option<DocActor> {
        (client.is_some() || model.is_some()).then(|| DocActor {
            client: client.clone(),
            model: model.clone(),
        })
    }
}

/// One row's admission inputs as `digest_admission_rows` or
/// `_ADMISSION_INPUTS` reads them: its row number, its scopes text (as
/// `str()` spells the column: `"None"` for NULL), the project its
/// `homes` row names, its writer's client and model, its BM25 length (0
/// where the digest did not answer).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AdmissionRow {
    pub rid: i64,
    pub scopes_text: String,
    pub home_project: Option<String>,
    pub client: Option<String>,
    pub model: Option<String>,
    pub length: i64,
}

/// A group as the grouping makes it: scopes, home, actor, positions.
type Made = (Vec<PyValue>, Option<String>, Option<DocActor>, Vec<usize>);

/// The memo's key: a store's path text and id.
pub type StoreKey = (String, String);

/// The columns a group is keyed on: scopes text, home, client, model.
pub type GroupKey = (String, Option<String>, Option<String>, Option<String>);

/// One group of rows with equal admission inputs.
#[derive(Clone, Debug, PartialEq)]
pub struct Group {
    pub scopes: Vec<PyValue>,
    pub home_project: Option<String>,
    pub actor: Option<DocActor>,
    pub mask: Mask,
}

/// `(seq, mac)` of the log's last memory or home row (`_memory_head`).
pub type Head = (i64, String);

/// A term's masks: body, body or scopes, and the version read at.
pub type TermMasks = (Mask, Mask, u64);

/// `_DocFreqInputs` without its terms, which [`Entry`] holds.
#[derive(Clone, Debug, PartialEq)]
pub struct DocFreqInputs {
    pub head: Head,
    pub groups: Vec<Group>,
    pub index: HashMap<i64, usize>,
    pub lengths: Option<Vec<i128>>,
    pub keys: Vec<GroupKey>,
    pub group_of: Option<Vec<i32>>,
    pub length_of: Option<Vec<i64>>,
    pub version: u64,
    pub changed: Vec<(u64, i64, usize)>,
}

/// `_load_json(text, None)`: the scopes a text parses to, `Ok(None)` for
/// a value that is not a list; a text that is not JSON raises.
pub fn load_scopes(text: &str) -> Result<Option<Vec<PyValue>>, DocFreqError> {
    let cps: Vec<u32> = text.chars().map(u32::from).collect();
    match &json::loads(&cps).map_err(DocFreqError::Py)? {
        PyValue::List(items) => Ok(Some(items.clone())),
        _ => Ok(None),
    }
}

impl DocFreqInputs {
    /// The grouping of `Store._read_doc_freq_inputs` over the rows read;
    /// `from_digest` says whether they carry BM25 lengths.
    pub fn from_rows(
        head: Head,
        rows: &[AdmissionRow],
        from_digest: bool,
    ) -> Result<DocFreqInputs, DocFreqError> {
        let mut index: HashMap<i64, usize> = HashMap::new();
        let mut numbers: HashMap<GroupKey, usize> = HashMap::new();
        let mut keys: Vec<GroupKey> = Vec::new();
        let mut made: Vec<Made> = Vec::new();
        let mut totals: Vec<i128> = Vec::new();
        let mut group_of = Vec::new();
        let mut length_of = Vec::new();
        for row in rows {
            let Some(scopes) = load_scopes(&row.scopes_text)? else {
                continue;
            };
            let position = index.len();
            index.insert(row.rid, position);
            let key: GroupKey = (
                row.scopes_text.clone(),
                row.home_project.clone(),
                row.client.clone(),
                row.model.clone(),
            );
            let number = *numbers.entry(key.clone()).or_insert_with(|| {
                keys.push(key);
                made.push((
                    scopes,
                    row.home_project.clone(),
                    DocActor::of(&row.client, &row.model),
                    Vec::new(),
                ));
                totals.push(0);
                made.len() - 1
            });
            made[number].3.push(position);
            totals[number] += i128::from(row.length);
            group_of.push(number as i32);
            length_of.push(row.length);
        }
        let width = index.len();
        let mut groups = Vec::with_capacity(made.len());
        for (scopes, home_project, actor, positions) in made {
            let mask = row_mask(&positions, width)?;
            groups.push(Group {
                scopes,
                home_project,
                actor,
                mask,
            });
        }
        Ok(DocFreqInputs {
            head,
            groups,
            index,
            lengths: from_digest.then_some(totals),
            keys,
            group_of: Some(group_of),
            length_of: Some(length_of),
            version: 0,
            changed: Vec::new(),
        })
    }
}

/// The store's side of the memo.
pub trait DocFreqSource {
    /// `Store._memory_head`.
    fn memory_head(&self) -> Result<Head, DocFreqError>;
    /// `(str(self._path), self._store_id)`.
    fn store_key(&self) -> (String, String);
    /// `self._tx_depth or self._conn.in_transaction`.
    fn in_transaction(&self) -> bool;
    /// The rows `_read_doc_freq_inputs` groups, and whether they came from
    /// the digest (with lengths) or from `_ADMISSION_INPUTS` (without).
    fn admission_rows(&self) -> Result<(Vec<AdmissionRow>, bool), DocFreqError>;
    /// `Store._read_doc_freq_inputs`.
    fn read_inputs(&self, head: &Head) -> Result<DocFreqInputs, DocFreqError> {
        let (rows, from_digest) = self.admission_rows()?;
        DocFreqInputs::from_rows(head.clone(), &rows, from_digest)
    }
    /// `store_index._advanced_inputs`: `entry` brought to `head` by the
    /// log's rows since, or None where it has to be read again.
    fn advanced_inputs(
        &self,
        _entry: &DocFreqInputs,
        _head: &Head,
    ) -> Result<Option<DocFreqInputs>, DocFreqError> {
        Ok(None)
    }
    /// `Store._term_masks`: the rows whose body matches, and whose body
    /// or scopes do.
    fn term_masks(&self, term: &str, inputs: &DocFreqInputs) -> Result<(Mask, Mask), DocFreqError>;
    /// The patch's statement: `SELECT rowid FROM memories_fts WHERE
    /// memories_fts MATCH ? AND rowid >= ? ORDER BY rowid DESC LIMIT ?`.
    fn fts_rowids(&self, expr: &str, low: i64, limit: usize) -> Result<Vec<i64>, DocFreqError>;
}

/// The memo's terms for one entry, most recently used last.
#[derive(Default)]
struct Terms {
    tick: u64,
    map: HashMap<String, (TermMasks, u64)>,
    order: std::collections::BTreeMap<u64, String>,
}

impl Terms {
    fn get_touch(&mut self, term: &str) -> Option<TermMasks> {
        let (masks, old) = self.map.get(term).cloned()?;
        self.touch(term, old);
        Some(masks)
    }

    fn touch(&mut self, term: &str, old: u64) {
        self.order.remove(&old);
        self.tick += 1;
        self.order.insert(self.tick, term.to_string());
        if let Some(slot) = self.map.get_mut(term) {
            slot.1 = self.tick;
        }
    }

    /// `update` then `move_to_end` each: a new term goes last either way.
    fn put(&mut self, term: &str, masks: TermMasks) {
        match self.map.get_mut(term) {
            Some(slot) => {
                slot.0 = masks;
                let old = slot.1;
                self.touch(term, old);
            }
            None => {
                self.tick += 1;
                self.order.insert(self.tick, term.to_string());
                self.map.insert(term.to_string(), (masks, self.tick));
            }
        }
    }

    fn trim(&mut self, bound: usize) {
        while self.map.len() > bound {
            let Some((_, oldest)) = self.order.pop_first() else {
                break;
            };
            self.map.remove(&oldest);
        }
    }

    fn copy(&self) -> Terms {
        Terms {
            tick: self.tick,
            map: self.map.clone(),
            order: self.order.clone(),
        }
    }
}

/// An entry of the memo: the inputs and the terms' masks.
pub struct Entry {
    pub inputs: DocFreqInputs,
    terms: Mutex<Terms>,
}

impl Entry {
    /// The terms kept, least recently used first.
    pub fn terms(&self) -> Vec<String> {
        lock(&self.terms).order.values().cloned().collect()
    }
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// The memo: stores, least recently used first, with their entries.
#[derive(Default)]
pub struct DocFreqMemo {
    stores: Mutex<Vec<(StoreKey, Arc<Entry>)>>,
    store_bound: usize,
    term_bound: usize,
}

/// The process-wide memo (`_DOC_FREQS`).
pub fn memo() -> &'static DocFreqMemo {
    static MEMO: OnceLock<DocFreqMemo> = OnceLock::new();
    MEMO.get_or_init(DocFreqMemo::new)
}

/// `(size, body_df, scope_df, average)`; the dfs in the terms' order.
pub type Statistics = (
    usize,
    Vec<(String, usize)>,
    Vec<(String, usize)>,
    Option<f64>,
);

/// What `ranking_statistics` answers through the memo.
#[derive(Clone, Debug, PartialEq)]
pub enum Ranked {
    /// No term, or no row admitted.
    Nothing,
    /// Inside a transaction: the uncached call answers instead.
    Uncached,
    Found(Statistics),
}

type Admit<'a> = dyn FnMut(&[PyValue], Option<&str>, Option<&DocActor>) -> bool + 'a;

impl DocFreqMemo {
    pub fn new() -> DocFreqMemo {
        DocFreqMemo::with_bounds(DOC_FREQ_STORES, DOC_FREQ_TERMS)
    }

    pub fn with_bounds(store_bound: usize, term_bound: usize) -> DocFreqMemo {
        DocFreqMemo {
            stores: Mutex::new(Vec::new()),
            store_bound,
            term_bound,
        }
    }

    /// `_clear_doc_freqs`.
    pub fn clear(&self) {
        lock(&self.stores).clear();
    }

    /// The keys kept, least recently read first.
    pub fn keys(&self) -> Vec<(String, String)> {
        lock(&self.stores).iter().map(|(k, _)| k.clone()).collect()
    }

    /// The entry kept for a key.
    pub fn entry(&self, key: &(String, String)) -> Option<Arc<Entry>> {
        lock(&self.stores)
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, e)| e.clone())
    }

    /// `Store.ranking_statistics`, the memoised half: the terms sorted and
    /// unique, empty ones dropped.
    pub fn ranking_statistics(
        &self,
        source: &dyn DocFreqSource,
        terms: &[String],
        admit: &mut Admit<'_>,
    ) -> Result<Ranked, DocFreqError> {
        let mut unique: Vec<String> = terms.iter().filter(|t| !t.is_empty()).cloned().collect();
        unique.sort();
        unique.dedup();
        if unique.is_empty() {
            return Ok(Ranked::Nothing);
        }
        if source.in_transaction() {
            return Ok(Ranked::Uncached);
        }
        Ok(match self.document_frequencies(source, &unique, admit)? {
            Some(found) => Ranked::Found(found),
            None => Ranked::Nothing,
        })
    }

    /// `_memoised_document_frequencies`.
    pub fn document_frequencies(
        &self,
        source: &dyn DocFreqSource,
        unique: &[String],
        admit: &mut Admit<'_>,
    ) -> Result<Option<Statistics>, DocFreqError> {
        let (entry, fresh, key, head) = self.current_inputs(source)?;
        let version = entry.inputs.version;
        let mut known: HashMap<&str, TermMasks> = HashMap::new();
        let mut behind: HashMap<&str, TermMasks> = HashMap::new();
        {
            let _stores = lock(&self.stores);
            let mut terms = lock(&entry.terms);
            for term in unique {
                if let Some(masks) = terms.get_touch(term) {
                    if masks.2 == version {
                        known.insert(term, masks);
                    } else {
                        behind.insert(term, masks);
                    }
                }
            }
        }
        let (admitted, length) = admitted_mask(&entry.inputs, admit);
        let mut found: Vec<(String, TermMasks)> = Vec::new();
        if !admitted.is_zero() {
            for term in unique {
                if known.contains_key(term.as_str()) {
                    continue;
                }
                let masks = match behind.get(term.as_str()) {
                    Some(old) => patched_masks(source, term, old, &entry.inputs)?,
                    None => {
                        let (body, either) = source.term_masks(term, &entry.inputs)?;
                        (body, either, version)
                    }
                };
                match found.iter_mut().find(|(t, _)| t == term) {
                    Some(slot) => slot.1 = masks,
                    None => found.push((term.clone(), masks)),
                }
            }
        }
        if (fresh || !found.is_empty()) && source.memory_head()? == head {
            self.keep(key, &entry, &found, fresh);
        }
        if admitted.is_zero() {
            return Ok(None);
        }
        let mut body_df = Vec::new();
        let mut scope_df = Vec::new();
        for term in unique {
            let masks = known
                .get(term.as_str())
                .or_else(|| found.iter().find(|(t, _)| t == term).map(|(_, m)| m))
                .expect("every term is known or found");
            let body = masks.0.and_count(&admitted);
            let any = masks.1.and_count(&admitted);
            if body > 0 {
                body_df.push((term.clone(), body));
            }
            if any > 0 {
                scope_df.push((term.clone(), any));
            }
        }
        let size = admitted.count();
        let average = match entry.inputs.lengths {
            Some(_) if size > 0 => Some(int_div(length, size as u64)),
            _ => None,
        };
        Ok(Some((size, body_df, scope_df, average)))
    }

    /// `_current_inputs`.
    fn current_inputs(
        &self,
        source: &dyn DocFreqSource,
    ) -> Result<(Arc<Entry>, bool, StoreKey, Head), DocFreqError> {
        let key = source.store_key();
        let head = source.memory_head()?;
        let kept = {
            let mut stores = lock(&self.stores);
            let at = stores.iter().position(|(k, _)| *k == key);
            match at {
                Some(i) if stores[i].1.inputs.head == head => {
                    let item = stores.remove(i);
                    let entry = item.1.clone();
                    stores.push(item);
                    return Ok((entry, false, key, head));
                }
                Some(i) => Some(stores[i].1.clone()),
                None => None,
            }
        };
        if let Some(kept) = kept {
            if let Some(inputs) = source.advanced_inputs(&kept.inputs, &head)? {
                let terms = {
                    let _stores = lock(&self.stores);
                    let copied = lock(&kept.terms).copy();
                    copied
                };
                let entry = Arc::new(Entry {
                    inputs,
                    terms: Mutex::new(terms),
                });
                return Ok((entry, true, key, head));
            }
        }
        let entry = Arc::new(Entry {
            inputs: source.read_inputs(&head)?,
            terms: Mutex::new(Terms::default()),
        });
        Ok((entry, true, key, head))
    }

    /// `_keep_doc_freqs`.
    fn keep(
        &self,
        key: (String, String),
        entry: &Arc<Entry>,
        found: &[(String, TermMasks)],
        fresh: bool,
    ) {
        let mut stores = lock(&self.stores);
        let at = stores.iter().position(|(k, _)| *k == key);
        let same = at.is_some_and(|i| Arc::ptr_eq(&stores[i].1, entry));
        if !same {
            if !fresh || at.is_some_and(|i| stores[i].1.inputs.head == entry.inputs.head) {
                return;
            }
            match at {
                // Assigning an existing key keeps its place.
                Some(i) => stores[i].1 = entry.clone(),
                None => stores.push((key.clone(), entry.clone())),
            }
        }
        {
            let mut terms = lock(&entry.terms);
            for (term, masks) in found {
                terms.put(term, masks.clone());
            }
            terms.trim(self.term_bound);
        }
        if let Some(i) = stores.iter().position(|(k, _)| *k == key) {
            let item = stores.remove(i);
            stores.push(item);
        }
        while stores.len() > self.store_bound {
            stores.remove(0);
        }
    }
}

/// Python's `int / int` for a nonzero `d`: the quotient rounded once to
/// the nearest double, ties to even. The quotient is taken to at least 55
/// bits, a remainder folds into its lowest bit, and the one conversion to
/// `f64` rounds; the power of two that scales it back is exact.
pub fn int_div(n: i128, d: u64) -> f64 {
    let a = n.unsigned_abs();
    if a == 0 {
        return 0.0;
    }
    let d = u128::from(d);
    let bits = |x: u128| 128 - x.leading_zeros() as i32;
    let k = 55 - (bits(a) - bits(d));
    let (q, r) = if k >= 0 {
        let num = a << k;
        (num / d, num % d)
    } else {
        let den = d << -k;
        (a / den, a % den)
    };
    let q = q | u128::from(r != 0);
    let magnitude = q as f64 * 2f64.powi(-k);
    if n < 0 {
        -magnitude
    } else {
        magnitude
    }
}

/// `_admitted_mask`: the rows whose group `admit` admits, and the sum of
/// their BM25 lengths (0 where the inputs carry none). `admit` runs once
/// per group, in the groups' order, an empty group included.
pub fn admitted_mask(inputs: &DocFreqInputs, admit: &mut Admit<'_>) -> (Mask, i128) {
    let mut admitted = Mask::default();
    let mut length = 0;
    for (number, group) in inputs.groups.iter().enumerate() {
        if admit(
            &group.scopes,
            group.home_project.as_deref(),
            group.actor.as_ref(),
        ) {
            admitted.or_with(&group.mask);
            if let Some(lengths) = &inputs.lengths {
                length += lengths[number];
            }
        }
    }
    (admitted, length)
}

/// `'"' + term.replace('"', '""') + '"'`.
pub fn quoted(term: &str) -> String {
    format!("\"{}\"", term.replace('"', "\"\""))
}

/// `_patched_masks`: masks read at an older version brought up to date
/// for the rows changed since, or read whole past the patch's bounds.
pub fn patched_masks(
    source: &dyn DocFreqSource,
    term: &str,
    masks: &TermMasks,
    inputs: &DocFreqInputs,
) -> Result<TermMasks, DocFreqError> {
    let (mut body, mut either, seen) = masks.clone();
    let mut rows: Vec<(i64, usize)> = Vec::new();
    for &(version, rid, position) in &inputs.changed {
        if version > seen && !rows.contains(&(rid, position)) {
            rows.push((rid, position));
        }
    }
    let whole = || -> Result<TermMasks, DocFreqError> {
        let (b, e) = source.term_masks(term, inputs)?;
        Ok((b, e, inputs.version))
    };
    if rows.is_empty() {
        return Ok((body, either, inputs.version));
    }
    if rows.len() > PATCH_ROWS {
        return whole();
    }
    let low = rows.iter().map(|r| r.0).min().unwrap_or(0);
    let quoted = quoted(term);
    let matching = |expr: &str| -> Result<Option<Vec<i64>>, DocFreqError> {
        let found = source.fts_rowids(expr, low, PATCH_WINDOW + 1)?;
        Ok((found.len() <= PATCH_WINDOW).then_some(found))
    };
    let Some(in_either) = matching(&quoted)? else {
        return whole();
    };
    let mut in_body = Vec::new();
    if rows.iter().any(|(rid, _)| in_either.contains(rid)) {
        match matching(&format!("body_fts : {quoted}"))? {
            Some(found) => in_body = found,
            None => return whole(),
        }
    }
    for &(rid, position) in &rows {
        if in_body.contains(&rid) {
            body.set(position);
        } else {
            body.clear(position);
        }
        if in_either.contains(&rid) {
            either.set(position);
        } else {
            either.clear(position);
        }
    }
    Ok((body, either, inputs.version))
}
