//! `search_dedup`: write-time dedup, Jaccard over raw content token sets.
//!
//! The token sets come from [`RawTokenCache`], the reference's bounded LRU
//! (`_RAW_TOKEN_SETS`) made a value the caller holds. Records reach the
//! scorer through [`Candidate`]; a store's digest through [`DigestStore`].
//! Every float is computed in the reference's order: a count divided by a
//! count (exact operands, one correctly rounded division), `min`/`max` as
//! Python picks between equal values, and `round(x, 4)` as CPython rounds.

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::{Arc, Mutex, MutexGuard};

use crate::datetime::DateTime;
use crate::lexicon::{expand_kebab, is_stopword, kebab_parts, strip_stopwords, tokenize};
use crate::models_text;
use crate::pyvalue::PyStr;
use crate::records::{Confidence, Memory, Tombstone};
use crate::search::num::{int_true_div, round4};

/// `HIGH_SIMILARITY`.
pub const HIGH_SIMILARITY: f64 = 0.75;
/// `MEDIUM_SIMILARITY`.
pub const MEDIUM_SIMILARITY: f64 = 0.40;
/// `_CONTAINMENT_MIN_TOKENS`.
pub const CONTAINMENT_MIN_TOKENS: usize = 8;
/// `_CONTAINMENT_CEILING`, computed as the reference computes it.
pub const CONTAINMENT_CEILING: f64 = (HIGH_SIMILARITY + MEDIUM_SIMILARITY) / 2.0;
/// `RAW_TOKEN_SET_ENTRIES`.
pub const RAW_TOKEN_SET_ENTRIES: i64 = 5000;

/// A token set. Its order (code point order) is only for showing it.
pub type TokenSet = BTreeSet<String>;

/// What the reference raises.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DedupError {
    /// `KeyError`: a record the store's light reader returned whose id the
    /// digest did not score (`scoring[record.id]`).
    MissingKey(PyStr),
    /// `KeyError`: `popitem` of an empty cache, under a negative bound.
    EmptyCache,
    /// `TypeError`: the sort compared a naive and an aware datetime.
    NaiveAware,
}

impl DedupError {
    /// The exception's class name.
    pub fn class(&self) -> &'static str {
        match self {
            DedupError::NaiveAware => "TypeError",
            _ => "KeyError",
        }
    }
}

/// `_content_token_set`: stopword-stripped tokens after kebab expansion.
pub fn content_token_set(text: &[u32]) -> TokenSet {
    strip_stopwords(&expand_kebab(&tokenize(text)))
        .into_iter()
        .collect()
}

/// `_raw_content_token_set`: stopword-stripped tokens, compounds whole.
pub fn raw_content_token_set(text: &[u32]) -> TokenSet {
    strip_stopwords(&tokenize(text)).into_iter().collect()
}

#[derive(Default)]
struct Lru {
    next: u64,
    map: HashMap<PyStr, (u64, Arc<TokenSet>)>,
    order: BTreeMap<u64, PyStr>,
}

/// `raw_content_tokens`' cache: keyed on the text, least recently used
/// first out past `entries` (`RAW_TOKEN_SET_ENTRIES`, a Python int the
/// reference's tests patch; a negative bound empties the cache and then
/// raises `KeyError`, as `popitem` of an empty `OrderedDict` does).
pub struct RawTokenCache {
    entries: i64,
    lru: Mutex<Lru>,
}

impl Default for RawTokenCache {
    fn default() -> Self {
        RawTokenCache::with_entries(RAW_TOKEN_SET_ENTRIES)
    }
}

impl RawTokenCache {
    pub fn with_entries(entries: i64) -> RawTokenCache {
        RawTokenCache {
            entries,
            lru: Mutex::new(Lru::default()),
        }
    }

    fn lock(&self) -> MutexGuard<'_, Lru> {
        self.lru.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// `clear_raw_token_set_cache`.
    pub fn clear(&self) {
        let mut lru = self.lock();
        lru.map.clear();
        lru.order.clear();
    }

    /// The keys held, least recently used first.
    pub fn keys(&self) -> Vec<PyStr> {
        self.lock().order.values().cloned().collect()
    }

    /// `raw_content_tokens`: the held set for an equal text (touched), or
    /// one built now and inserted. The tokenizing runs outside the lock.
    pub fn tokens(&self, text: &[u32]) -> Result<Arc<TokenSet>, DedupError> {
        {
            let mut lru = self.lock();
            let lru = &mut *lru;
            if let Some((seq, value)) = lru.map.get_mut(text) {
                lru.order.remove(seq);
                *seq = lru.next;
                lru.order.insert(lru.next, text.to_vec());
                lru.next += 1;
                return Ok(value.clone());
            }
        }
        self.store(text, Arc::new(raw_content_token_set(text)))
    }

    /// The second lock section of `tokens`: `d[key] = value`, then the
    /// eviction. A caller that tokenized while another caller inserted the
    /// same text reaches the present-key branch, which keeps the key's place.
    pub fn store(&self, text: &[u32], tokens: Arc<TokenSet>) -> Result<Arc<TokenSet>, DedupError> {
        let mut lru = self.lock();
        let lru = &mut *lru;
        if let Some((_, value)) = lru.map.get_mut(text) {
            // `d[key] = value` on a present key keeps its place.
            *value = tokens.clone();
        } else {
            lru.map.insert(text.to_vec(), (lru.next, tokens.clone()));
            lru.order.insert(lru.next, text.to_vec());
            lru.next += 1;
        }
        while lru.map.len() as i128 > i128::from(self.entries) {
            let Some((_, oldest)) = lru.order.pop_first() else {
                return Err(DedupError::EmptyCache);
            };
            lru.map.remove(&oldest);
        }
        Ok(tokens)
    }
}

fn expanded(raw: &TokenSet, other: &TokenSet) -> TokenSet {
    let mut out = raw.clone();
    for tok in raw {
        if !other.contains(tok) {
            out.extend(kebab_parts(tok).into_iter().filter(|p| !is_stopword(p)));
        }
    }
    out
}

/// Python's `max(a, b)` of floats: `a` unless `b > a`.
fn py_max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// Python's `min(a, b)` of floats: `a` unless `b < a`.
fn py_min(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}

/// `_pairwise_content_jaccard`.
pub fn pairwise_content_jaccard(raw_a: &TokenSet, raw_b: &TokenSet) -> f64 {
    if raw_a.is_empty() || raw_b.is_empty() {
        return 0.0;
    }
    let a = expanded(raw_a, raw_b);
    let b = expanded(raw_b, raw_a);
    let shared = a.intersection(&b).count();
    if shared == 0 {
        return 0.0;
    }
    let jaccard = shared as f64 / (a.len() + b.len() - shared) as f64;
    let smaller = if a.len() <= b.len() { a.len() } else { b.len() };
    if smaller >= CONTAINMENT_MIN_TOKENS {
        let containment = shared as f64 / smaller as f64;
        if containment >= MEDIUM_SIMILARITY {
            return py_max(jaccard, py_min(containment, CONTAINMENT_CEILING));
        }
    }
    jaccard
}

/// `similarity_bound`, for counts a store holds (any `i64`).
pub fn similarity_bound(shared: i64, n: i64, m: i64) -> f64 {
    if shared <= 0 || n <= 0 || m <= 0 {
        return 0.0;
    }
    let (s, n, m) = (i128::from(shared), i128::from(n), i128::from(m));
    let mut bound = py_min(1.0, int_true_div(s, n.max(m)));
    if n + m - s > 0 {
        bound = py_min(bound, int_true_div(s, n + m - s));
    }
    let contained = int_true_div(s, n.min(m));
    if contained >= MEDIUM_SIMILARITY {
        bound = py_max(bound, py_min(contained, CONTAINMENT_CEILING));
    }
    bound
}

/// `models.snippet_for(body)` with its default 200 characters
/// (`models_text::snippet_for`).
pub fn snippet_for(body: &[u32]) -> PyStr {
    models_text::snippet_for(body, models_text::SNIPPET_MAX_CHARS)
}

/// A record a hit is built from: `Memory`, `TombstonedMemory`, a store's
/// `LightRecord`.
pub trait Candidate {
    fn id(&self) -> &[u32];
    fn scopes(&self) -> &[PyStr];
    fn confidence(&self) -> Confidence;
    fn body(&self) -> &[u32];
    fn created(&self) -> DateTime;
    fn updated(&self) -> DateTime;
    /// `removed`, read by the tombstone hit only.
    fn removed(&self) -> Option<DateTime> {
        None
    }
    /// `removed_reason`, read by the tombstone hit only.
    fn removed_reason(&self) -> Option<&[u32]> {
        None
    }
}

impl Candidate for Memory {
    fn id(&self) -> &[u32] {
        &self.fields.id
    }
    fn scopes(&self) -> &[PyStr] {
        &self.fields.scopes
    }
    fn confidence(&self) -> Confidence {
        self.fields.confidence
    }
    fn body(&self) -> &[u32] {
        &self.fields.body
    }
    fn created(&self) -> DateTime {
        self.fields.created
    }
    fn updated(&self) -> DateTime {
        self.fields.updated
    }
}

impl Candidate for Tombstone {
    fn id(&self) -> &[u32] {
        &self.fields.id
    }
    fn scopes(&self) -> &[PyStr] {
        &self.fields.scopes
    }
    fn confidence(&self) -> Confidence {
        self.fields.confidence
    }
    fn body(&self) -> &[u32] {
        &self.fields.body
    }
    fn created(&self) -> DateTime {
        self.fields.created
    }
    fn updated(&self) -> DateTime {
        self.fields.updated
    }
    fn removed(&self) -> Option<DateTime> {
        Some(self.removed)
    }
    fn removed_reason(&self) -> Option<&[u32]> {
        Some(&self.removed_reason)
    }
}

/// `models.SimilarHit`.
#[derive(Clone, Debug, PartialEq)]
pub struct SimilarHit {
    pub id: PyStr,
    pub scopes: Vec<PyStr>,
    pub confidence: Confidence,
    pub snippet: PyStr,
    pub similarity: f64,
    pub relevance: &'static str,
    pub created: DateTime,
    pub updated: DateTime,
    pub removed_at: Option<DateTime>,
    pub removed_reason: Option<PyStr>,
}

/// Which pass: the active one (`_build_active_hit`, `_active_sort_key`,
/// `high`/`medium`) or the tombstone one (`-removed`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Pass {
    Active,
    Removed,
}

fn build_hit<R: Candidate>(
    record: &R,
    similarity: f64,
    relevance: &'static str,
    pass: Pass,
) -> SimilarHit {
    let removed = pass == Pass::Removed;
    SimilarHit {
        id: record.id().to_vec(),
        scopes: record.scopes().to_vec(),
        confidence: record.confidence(),
        snippet: snippet_for(record.body()),
        similarity,
        relevance,
        created: record.created(),
        updated: record.updated(),
        removed_at: if removed { record.removed() } else { None },
        removed_reason: if removed {
            record.removed_reason().map(<[u32]>::to_vec)
        } else {
            None
        },
    }
}

/// Python's order of two datetimes both naive or both aware.
fn dt_cmp(a: &DateTime, b: &DateTime) -> Ordering {
    match (a.instant_us(), b.instant_us()) {
        (Some(x), Some(y)) => x.cmp(&y),
        _ => a.wall_micros().cmp(&b.wall_micros()),
    }
}

/// `hits.sort(key=sort_key, reverse=True)`. The keys are `(similarity,
/// datetime)` tuples; two with equal similarity compare their datetimes,
/// which raises for a naive against an aware one. Any comparison sort
/// that orders a group of equal similarity holding both kinds must compare
/// one of each (else it orders them alike whether every naive one comes
/// first or last), so `list.sort` raises exactly when such a group exists;
/// otherwise its answer is the stable descending order.
fn sort_hits(hits: &mut [SimilarHit], pass: Pass) -> Result<(), DedupError> {
    let key = |h: &SimilarHit| match pass {
        Pass::Active => h.updated,
        Pass::Removed => h.removed_at.unwrap_or(h.updated),
    };
    let mut kinds: HashMap<u64, (bool, bool)> = HashMap::new();
    for h in hits.iter() {
        let seen = kinds.entry(h.similarity.to_bits()).or_default();
        if key(h).offset_us.is_some() {
            seen.1 = true;
        } else {
            seen.0 = true;
        }
        if seen.0 && seen.1 {
            return Err(DedupError::NaiveAware);
        }
    }
    hits.sort_by(|a, b| {
        b.similarity
            .partial_cmp(&a.similarity)
            .unwrap_or(Ordering::Equal)
            .then_with(|| dt_cmp(&key(b), &key(a)))
    });
    Ok(())
}

type TokensOf<'a, R> = &'a dyn Fn(&R) -> Result<Arc<TokenSet>, DedupError>;

/// `_score_similar_jaccard`.
fn score_similar<R: Candidate>(
    cache: &RawTokenCache,
    new_body: &[u32],
    existing: &[R],
    (high, medium): (f64, f64),
    pass: Pass,
    tokens_of: Option<TokensOf<'_, R>>,
) -> Result<Vec<SimilarHit>, DedupError> {
    let new_tokens = cache.tokens(new_body)?;
    if new_tokens.is_empty() {
        return Ok(Vec::new());
    }
    let (high_label, medium_label) = match pass {
        Pass::Active => ("high", "medium"),
        Pass::Removed => ("high-removed", "medium-removed"),
    };
    let mut hits = Vec::new();
    for record in existing {
        let tokens = match tokens_of {
            Some(f) => f(record)?,
            None => cache.tokens(record.body())?,
        };
        if tokens.is_empty() {
            continue;
        }
        let similarity = pairwise_content_jaccard(&new_tokens, &tokens);
        if similarity <= 0.0 {
            continue;
        }
        let relevance = if similarity >= high {
            high_label
        } else if similarity >= medium {
            medium_label
        } else {
            continue;
        };
        hits.push(build_hit(record, round4(similarity), relevance, pass));
    }
    sort_hits(&mut hits, pass)?;
    Ok(hits)
}

fn thresholds(high: Option<f64>, medium: Option<f64>) -> (f64, f64) {
    (
        high.unwrap_or(HIGH_SIMILARITY),
        medium.unwrap_or(MEDIUM_SIMILARITY),
    )
}

/// `find_similar` (and `_find_similar_jaccard`).
pub fn find_similar<R: Candidate>(
    cache: &RawTokenCache,
    new_body: &[u32],
    existing: &[R],
    high: Option<f64>,
    medium: Option<f64>,
) -> Result<Vec<SimilarHit>, DedupError> {
    let t = thresholds(high, medium);
    score_similar(cache, new_body, existing, t, Pass::Active, None)
}

/// `find_similar_tombstones` (and `_find_similar_tombstones_jaccard`):
/// an empty list answers before the body is tokenized.
pub fn find_similar_tombstones<R: Candidate>(
    cache: &RawTokenCache,
    new_body: &[u32],
    tombstoned: &[R],
    high: Option<f64>,
    medium: Option<f64>,
) -> Result<Vec<SimilarHit>, DedupError> {
    if tombstoned.is_empty() {
        return Ok(Vec::new());
    }
    let t = thresholds(high, medium);
    score_similar(cache, new_body, tombstoned, t, Pass::Removed, None)
}

/// One record as a store's digest holds it (`DigestDoc`): the fields the
/// dedup reads.
#[derive(Clone, Debug)]
pub struct DigestDoc {
    pub id: PyStr,
    pub tokens: Arc<TokenSet>,
}

/// The memo key `("similar", removed, new_body)`.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct MemoKey {
    pub removed: bool,
    pub body: PyStr,
}

/// What the answer of one memo computation is.
pub type Computed = Result<Option<Vec<SimilarHit>>, DedupError>;

/// The store methods `_similar_in` calls, by their reference names.
pub trait DigestStore: Sized {
    type Light: Candidate;
    type Active: Candidate;
    type Removed: Candidate;
    fn digest_candidates(
        &mut self,
        tokens: &TokenSet,
        removed: bool,
        floor: f64,
    ) -> Option<Vec<DigestDoc>>;
    /// `light_records(ids, removed=...)`; `ids` are the scoring dict's keys.
    fn light_records(&mut self, ids: &[PyStr], removed: bool) -> Vec<Self::Light>;
    fn digest_memo(
        &mut self,
        key: &MemoKey,
        compute: &mut dyn FnMut(&mut Self) -> Computed,
    ) -> Computed;
    fn load_all(&mut self) -> Vec<Self::Active>;
    fn load_tombstones(&mut self) -> Vec<Self::Removed>;
}

fn similar_in<S: DigestStore>(
    cache: &RawTokenCache,
    store: &mut S,
    new_body: &[u32],
    removed: bool,
) -> Result<Vec<SimilarHit>, DedupError> {
    let new_tokens = cache.tokens(new_body)?;
    if new_tokens.is_empty() {
        return Ok(Vec::new());
    }
    let pass = if removed { Pass::Removed } else { Pass::Active };
    let mut compute = |store: &mut S| -> Computed {
        let Some(docs) = store.digest_candidates(&new_tokens, removed, MEDIUM_SIMILARITY) else {
            return Ok(None);
        };
        let mut order: Vec<PyStr> = Vec::new();
        let mut scoring: HashMap<PyStr, Arc<TokenSet>> = HashMap::new();
        for doc in docs {
            if pairwise_content_jaccard(&new_tokens, &doc.tokens) >= MEDIUM_SIMILARITY {
                if !scoring.contains_key(&doc.id) {
                    order.push(doc.id.clone());
                }
                scoring.insert(doc.id, doc.tokens);
            }
        }
        let records = store.light_records(&order, removed);
        let tokens_of = |r: &S::Light| {
            scoring
                .get(r.id())
                .cloned()
                .ok_or_else(|| DedupError::MissingKey(r.id().to_vec()))
        };
        let t = (HIGH_SIMILARITY, MEDIUM_SIMILARITY);
        score_similar(cache, new_body, &records, t, pass, Some(&tokens_of)).map(Some)
    };
    let key = MemoKey {
        removed,
        body: new_body.to_vec(),
    };
    if let Some(hits) = store.digest_memo(&key, &mut compute)? {
        return Ok(hits);
    }
    if removed {
        let all = store.load_tombstones();
        find_similar_tombstones(cache, new_body, &all, None, None)
    } else {
        let all = store.load_all();
        find_similar(cache, new_body, &all, None, None)
    }
}

/// `find_similar_in`.
pub fn find_similar_in<S: DigestStore>(
    cache: &RawTokenCache,
    store: &mut S,
    new_body: &[u32],
) -> Result<Vec<SimilarHit>, DedupError> {
    similar_in(cache, store, new_body, false)
}

/// `find_similar_tombstones_in`.
pub fn find_similar_tombstones_in<S: DigestStore>(
    cache: &RawTokenCache,
    store: &mut S,
    new_body: &[u32],
) -> Result<Vec<SimilarHit>, DedupError> {
    similar_in(cache, store, new_body, true)
}
