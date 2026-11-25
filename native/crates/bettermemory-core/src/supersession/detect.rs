//! Unit supersession-1b: the detector (`supersession.py` 520-797).
//!
//! [`detect_supersession`] scores every claim-sized record it is given;
//! [`detect_supersession_in`] reads a store through [`SupersessionStore`]
//! (the reference's `digest_shares`, `digest_tokens_of`, `bodies_of`,
//! `digest_memo` and `load_all`), scoring records in the order of the
//! bound their shared count puts on their score and reading a body only
//! when the rule reaches it. Both hand the scored records to one rule
//! (`_rule`), which reads them by score rounded to four places, highest
//! first, then id, and stops once a list holds [`MAX_LINKS_PER_WRITE`].
//!
//! Token sets come through the caller's [`RawTokenCache`], read where the
//! reference reads `raw_content_tokens` (`claim_sized` included), so the
//! cache ends in the reference's order.

use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;

use super::{
    anchored_values, change_cues, claim_sized, content_stream, context, strip_provenance,
    surface_form, surface_forms, value_tokens, values_are_kin, SupersessionError,
    SupersessionMatch, SupersessionReport, MAX_LINKS_PER_WRITE, MIN_CONFLICT_JACCARD,
    MIN_CONFLICT_KIN_JACCARD, MIN_CONTEXT_SHARED, MIN_JACCARD,
};
use crate::models_text;
use crate::pyvalue::{type_name, PyStr, PyValue};
use crate::search::num::round4;
use crate::search_dedup::{
    pairwise_content_jaccard, similarity_bound, Candidate, DedupError, RawTokenCache, TokenSet,
};
use crate::store::digest_query::DigestShare;
use crate::unicode;

/// `_SCORE_BATCH`: the records `_best_first` scores per read of tokens.
pub const SCORE_BATCH: usize = 64;
/// `snippet_for(old_body, max_chars=100)`: a match's summary.
pub const SUMMARY_CHARS: i64 = 100;

/// What stops a detection.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DetectError {
    /// A feature's pattern failed inside the port.
    Features(SupersessionError),
    /// The token cache's `KeyError` under a negative bound.
    Tokens(DedupError),
    /// `TypeError`: the heap compared two entries of one id, score and
    /// token set, and so the two records' body readers (functions).
    Unorderable,
    /// A share's claim that is not an `int` within `i64` (its type's
    /// name): declined. The reference's `DigestShare.claim` is always
    /// `int(row[2])`, a token count.
    Claim(&'static str),
}

impl DetectError {
    /// The exception's class name, or `NotPorted` for a decline.
    pub fn class(&self) -> &'static str {
        match self {
            DetectError::Features(_) => "Fault",
            DetectError::Tokens(e) => e.class(),
            DetectError::Unorderable => "TypeError",
            DetectError::Claim(_) => "NotPorted",
        }
    }

    /// The exception's text where the reference raises.
    pub fn message(&self) -> String {
        match self {
            DetectError::Unorderable => {
                "'<' not supported between instances of 'function' and 'function'".to_string()
            }
            DetectError::Claim(name) => format!("a claim of type {name}"),
            other => format!("{other:?}"),
        }
    }
}

impl From<SupersessionError> for DetectError {
    fn from(e: SupersessionError) -> Self {
        DetectError::Features(e)
    }
}

impl From<DedupError> for DetectError {
    fn from(e: DedupError) -> Self {
        DetectError::Tokens(e)
    }
}

/// The memo key `("supersession", new_body, frozenset(exclude_ids))`.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct SupersessionMemoKey {
    pub body: PyStr,
    pub exclude: BTreeSet<PyStr>,
}

/// What one memo computation answers: None when the digest is not there.
pub type Detected = Result<Option<SupersessionReport>, DetectError>;

/// The store methods `detect_supersession_in` calls, by their reference
/// names. `digest_tokens_of` is called with its default `removed=False`.
/// Ids come in the order the reference's generators yield them, repeats
/// kept; a map leaves out an id it does not hold.
pub trait SupersessionStore: Sized {
    type Active: Candidate;
    fn digest_shares(
        &mut self,
        tokens: &TokenSet,
        removed: bool,
        floor: f64,
        claims: bool,
    ) -> Option<Vec<DigestShare>>;
    fn digest_tokens_of(&mut self, ids: &[String]) -> HashMap<String, Arc<TokenSet>>;
    fn bodies_of(&mut self, ids: &[String]) -> HashMap<String, PyStr>;
    fn digest_memo(
        &mut self,
        key: &SupersessionMemoKey,
        compute: &mut dyn FnMut(&mut Self) -> Detected,
    ) -> Detected;
    fn load_all(&mut self) -> Vec<Self::Active>;
}

/// `_one_claim_replaced`: the first match, then each later one whose old
/// value, lowered (`str.lower`), is the first one's.
pub fn one_claim_replaced(matches: Vec<SupersessionMatch>) -> Vec<SupersessionMatch> {
    let mut rest = matches.into_iter();
    let Some(first) = rest.next() else {
        return Vec::new();
    };
    let replaced = unicode::lower(&first.old_value);
    let mut out = vec![first];
    out.extend(rest.filter(|m| unicode::lower(&m.old_value) == replaced));
    out
}

/// `_floor(cues)`: `MIN_JACCARD` with a cue, else
/// `min(MIN_CONFLICT_JACCARD, MIN_CONFLICT_KIN_JACCARD)`.
pub fn floor(cues: &[PyStr]) -> f64 {
    if !cues.is_empty() {
        MIN_JACCARD
    } else if MIN_CONFLICT_KIN_JACCARD < MIN_CONFLICT_JACCARD {
        MIN_CONFLICT_KIN_JACCARD
    } else {
        MIN_CONFLICT_JACCARD
    }
}

fn not_eligible() -> SupersessionReport {
    SupersessionReport {
        eligible: false,
        supersedes: Vec::new(),
        conflicts: Vec::new(),
    }
}

/// `claim_sized(body)`, with its read of `raw_content_tokens(stripped)`
/// made through the cache.
fn claim_sized_in(cache: &RawTokenCache, body: &[u32]) -> Result<bool, DetectError> {
    let stripped = strip_provenance(body);
    cache.tokens(&stripped)?;
    Ok(claim_sized(&stripped))
}

/// How `_Old.body` reads a record's body: held, or `bodies_of([id])` when
/// the rule first asks.
#[derive(Clone, Debug)]
enum OldBody {
    Held(PyStr),
    Stored(String),
}

/// `_Old`: a stored record as the rule reads it.
#[derive(Clone, Debug)]
struct Old {
    id: PyStr,
    raw: Arc<TokenSet>,
    body: OldBody,
}

/// The scored records in the rule's order, and the reader of a body.
trait Ordered {
    fn next_scored(&mut self) -> Result<Option<(f64, Old)>, DetectError>;
    fn body_of(&mut self, old: &Old) -> Option<PyStr>;
}

/// `_detect`'s sorted list.
struct Scan(std::vec::IntoIter<(f64, Old)>);

impl Ordered for Scan {
    fn next_scored(&mut self) -> Result<Option<(f64, Old)>, DetectError> {
        Ok(self.0.next())
    }
    fn body_of(&mut self, old: &Old) -> Option<PyStr> {
        match &old.body {
            OldBody::Held(body) => Some(body.clone()),
            OldBody::Stored(_) => None,
        }
    }
}

/// `detect_supersession`, with `_claims_among` and `_detect`: every
/// claim-sized record not excluded is scored, those at the floor or over
/// sorted by `(-round(score, 4), id)` (stable: one id twice keeps its
/// order), then read by the rule.
pub fn detect_supersession<R: Candidate>(
    cache: &RawTokenCache,
    new_body: &[u32],
    existing: &[R],
    exclude_ids: &[PyStr],
) -> Result<SupersessionReport, DetectError> {
    let body = strip_provenance(new_body);
    if !claim_sized_in(cache, &body)? {
        return Ok(not_eligible());
    }
    let skip: BTreeSet<&[u32]> = exclude_ids.iter().map(Vec::as_slice).collect();
    let new_raw = cache.tokens(&body)?;
    let floor = floor(&change_cues(&body)?);
    let mut scored: Vec<(f64, f64, Old)> = Vec::new();
    for memory in existing {
        if skip.contains(&memory.id()) {
            continue;
        }
        let old_body = strip_provenance(memory.body());
        if !claim_sized_in(cache, &old_body)? {
            continue;
        }
        let raw = cache.tokens(&old_body)?;
        let similarity = pairwise_content_jaccard(&new_raw, &raw);
        if similarity >= floor {
            let old = Old {
                id: memory.id().to_vec(),
                raw,
                body: OldBody::Held(old_body),
            };
            scored.push((-round4(similarity), similarity, old));
        }
    }
    scored.sort_by(|a, b| a.0.total_cmp(&b.0).then_with(|| a.2.id.cmp(&b.2.id)));
    let ordered: Vec<(f64, Old)> = scored.into_iter().map(|(_, s, old)| (s, old)).collect();
    rule(cache, &body, &mut Scan(ordered.into_iter()))
}

/// A share's claim as `similarity_bound` takes it.
fn claim_count(claim: &PyValue) -> Result<i64, DetectError> {
    match claim {
        PyValue::Int(text) => text.parse().map_err(|_| DetectError::Claim("int")),
        PyValue::Bool(b) => Ok(i64::from(*b)),
        other => Err(DetectError::Claim(type_name(other))),
    }
}

/// One heap entry: `(-round(similarity, 4), id, similarity, old)`.
struct Entry {
    key: f64,
    id: PyStr,
    similarity: f64,
    old: Old,
}

/// Python's `<` of two entries: the first unequal item decides. Past an
/// equal id and score, `_Old` compares its token sets (`<` is the proper
/// subset) and then its body readers, two distinct functions, which raises.
fn entry_lt(a: &Entry, b: &Entry) -> Result<bool, DetectError> {
    if a.key != b.key {
        return Ok(a.key < b.key);
    }
    if a.id != b.id {
        return Ok(a.id < b.id);
    }
    if a.similarity != b.similarity {
        return Ok(a.similarity < b.similarity);
    }
    if a.old.id != b.old.id {
        return Ok(a.old.id < b.old.id);
    }
    if a.old.raw != b.old.raw {
        return Ok(a.old.raw.is_subset(&b.old.raw));
    }
    Err(DetectError::Unorderable)
}

/// `heapq`'s `_siftdown` (CPython's `_heapq.siftdown`).
fn sift_down(heap: &mut [Entry], start: usize, mut pos: usize) -> Result<(), DetectError> {
    while pos > start {
        let parent = (pos - 1) >> 1;
        if !entry_lt(&heap[pos], &heap[parent])? {
            break;
        }
        heap.swap(pos, parent);
        pos = parent;
    }
    Ok(())
}

/// `heapq`'s `_siftup`: the smaller child up to a leaf, then back down.
fn sift_up(heap: &mut [Entry], start: usize) -> Result<(), DetectError> {
    let end = heap.len();
    let mut pos = start;
    while pos < end >> 1 {
        let mut child = 2 * pos + 1;
        if child + 1 < end && !entry_lt(&heap[child], &heap[child + 1])? {
            child += 1;
        }
        heap.swap(child, pos);
        pos = child;
    }
    sift_down(heap, start, pos)
}

fn heap_push(heap: &mut Vec<Entry>, entry: Entry) -> Result<(), DetectError> {
    heap.push(entry);
    let last = heap.len() - 1;
    sift_down(heap, 0, last)
}

fn heap_pop(heap: &mut Vec<Entry>) -> Result<Option<Entry>, DetectError> {
    let Some(last) = heap.pop() else {
        return Ok(None);
    };
    if heap.is_empty() {
        return Ok(Some(last));
    }
    let top = std::mem::replace(&mut heap[0], last);
    sift_up(heap, 0)?;
    Ok(Some(top))
}

/// `_best_first`, a generator: the shares are bounded and sorted when
/// first pulled, then scored a batch at a time, and a scored record is
/// handed on once its rounded score is strictly above the rounded bound
/// of the next record not scored.
struct BestFirst<'s, S> {
    store: &'s mut S,
    cache: &'s RawTokenCache,
    new_raw: Arc<TokenSet>,
    floor: f64,
    shares: Vec<DigestShare>,
    bounded: Option<Vec<(f64, DigestShare)>>,
    total: usize,
    position: usize,
    heap: Vec<Entry>,
}

impl<S: SupersessionStore> BestFirst<'_, S> {
    fn bound(&mut self) -> Result<(), DetectError> {
        let n = self.new_raw.len() as i64;
        let mut bounded = Vec::with_capacity(self.shares.len());
        for s in self.shares.drain(..) {
            bounded.push((similarity_bound(s.shared, n, claim_count(&s.claim)?), s));
        }
        // `sorted(key=-bound)`: stable, so equal bounds keep their order.
        bounded.sort_by(|a, b| (-a.0).total_cmp(&-b.0));
        let floor = self.floor;
        self.total = bounded
            .iter()
            .position(|(bound, _)| *bound < floor)
            .unwrap_or(bounded.len());
        self.bounded = Some(bounded);
        Ok(())
    }

    /// One batch from `position`: the tokens of its unstamped records, the
    /// bodies of its stamped ones, each record scored and those at the
    /// floor pushed. The batch may reach past `total`.
    fn read_batch(&mut self) -> Result<(), DetectError> {
        let bounded = self.bounded.as_deref().unwrap_or_default();
        let end = (self.position + SCORE_BATCH).min(bounded.len());
        let chunk: Vec<DigestShare> = bounded[self.position..end]
            .iter()
            .map(|(_, s)| s.clone())
            .collect();
        self.position = end;
        let ids = |stamped: bool| -> Vec<String> {
            chunk
                .iter()
                .filter(|s| s.stamped == stamped)
                .map(|s| s.id.clone())
                .collect()
        };
        let tokens = self.store.digest_tokens_of(&ids(false));
        let stamped = self.store.bodies_of(&ids(true));
        for s in chunk {
            let old = if !s.stamped {
                let Some(raw) = tokens.get(&s.id) else {
                    continue;
                };
                Old {
                    id: unicode::cps(&s.id),
                    raw: raw.clone(),
                    body: OldBody::Stored(s.id.clone()),
                }
            } else if let Some(body) = stamped.get(&s.id) {
                let old_body = strip_provenance(body);
                Old {
                    id: unicode::cps(&s.id),
                    raw: self.cache.tokens(&old_body)?,
                    body: OldBody::Held(old_body),
                }
            } else {
                continue;
            };
            let similarity = pairwise_content_jaccard(&self.new_raw, &old.raw);
            if similarity >= self.floor {
                let entry = Entry {
                    key: -round4(similarity),
                    id: old.id.clone(),
                    similarity,
                    old,
                };
                heap_push(&mut self.heap, entry)?;
            }
        }
        Ok(())
    }
}

impl<S: SupersessionStore> Ordered for BestFirst<'_, S> {
    fn next_scored(&mut self) -> Result<Option<(f64, Old)>, DetectError> {
        if self.bounded.is_none() {
            self.bound()?;
        }
        loop {
            if self.position >= self.total && self.heap.is_empty() {
                return Ok(None);
            }
            let ceiling = match &self.bounded {
                Some(b) if self.position < self.total => round4(b[self.position].0),
                _ => -1.0,
            };
            if self.heap.first().is_some_and(|top| -top.key > ceiling) {
                if let Some(e) = heap_pop(&mut self.heap)? {
                    return Ok(Some((e.similarity, e.old)));
                }
            }
            if self.position >= self.total {
                continue;
            }
            self.read_batch()?;
        }
    }

    fn body_of(&mut self, old: &Old) -> Option<PyStr> {
        match &old.body {
            OldBody::Held(body) => Some(body.clone()),
            OldBody::Stored(id) => self.store.bodies_of(std::slice::from_ref(id)).remove(id),
        }
    }
}

/// `detect_supersession_in`: the digest's records best first through the
/// rule, under the store's memo; a memo answer of None (no digest) falls
/// back to [`detect_supersession`] over `load_all()`.
pub fn detect_supersession_in<S: SupersessionStore>(
    cache: &RawTokenCache,
    store: &mut S,
    new_body: &[u32],
    exclude_ids: &[PyStr],
) -> Result<SupersessionReport, DetectError> {
    let body = strip_provenance(new_body);
    if !claim_sized_in(cache, &body)? {
        return Ok(not_eligible());
    }
    let skip: BTreeSet<PyStr> = exclude_ids.iter().cloned().collect();
    let key = SupersessionMemoKey {
        body: new_body.to_vec(),
        exclude: skip.clone(),
    };
    let mut compute = |store: &mut S| -> Detected {
        let new_raw = cache.tokens(&body)?;
        let floor = floor(&change_cues(&body)?);
        let Some(shares) = store.digest_shares(&new_raw, false, floor, true) else {
            return Ok(None);
        };
        let shares = shares
            .into_iter()
            .filter(|s| !skip.contains(&unicode::cps(&s.id)))
            .collect();
        let mut ordered = BestFirst {
            store,
            cache,
            new_raw,
            floor,
            shares,
            bounded: None,
            total: 0,
            position: 0,
            heap: Vec::new(),
        };
        rule(cache, &body, &mut ordered).map(Some)
    };
    if let Some(report) = store.digest_memo(&key, &mut compute)? {
        return Ok(report);
    }
    let all = store.load_all();
    detect_supersession(cache, new_body, &all, exclude_ids)
}

/// `_rule`: each record in `ordered`'s order, until a list holds
/// `MAX_LINKS_PER_WRITE`.
fn rule(
    cache: &RawTokenCache,
    body: &[u32],
    ordered: &mut dyn Ordered,
) -> Result<SupersessionReport, DetectError> {
    let new_raw = cache.tokens(body)?;
    let cues = change_cues(body)?;
    let mut new_values_all = value_tokens(body, &new_raw)?;
    if !cues.is_empty() {
        new_values_all.extend(anchored_values(body)?.intersection(&new_raw).cloned());
    }
    let new_stream = content_stream(body);
    let new_surface = surface_forms(body)?;

    let mut supersedes: Vec<SupersessionMatch> = Vec::new();
    let mut conflicts: Vec<SupersessionMatch> = Vec::new();
    let mut replaced: Option<PyStr> = None;
    while let Some((similarity, old)) = ordered.next_scored()? {
        let new_values: BTreeSet<String> = new_values_all.difference(&old.raw).cloned().collect();
        if new_values.is_empty() {
            continue;
        }
        let Some(old_body) = ordered.body_of(&old) else {
            continue;
        };
        let old_values: BTreeSet<String> = value_tokens(&old_body, &old.raw)?
            .difference(&new_raw)
            .cloned()
            .collect();
        if old_values.is_empty() {
            continue;
        }

        let mut kin: Vec<(&String, &String)> = Vec::new();
        for a in &new_values {
            for b in &old_values {
                if values_are_kin(a, b)? {
                    kin.push((a, b));
                }
            }
        }
        kin.sort();
        let mut pair = kin.first().copied();
        let mut evidence = pair.map(|_| "kin");
        if pair.is_none() {
            let old_stream = content_stream(&old_body);
            'olds: for b in &old_values {
                if context(&old_stream, b).intersection(&new_raw).count() < MIN_CONTEXT_SHARED {
                    continue;
                }
                for a in &new_values {
                    if context(&new_stream, a).intersection(&old.raw).count() >= MIN_CONTEXT_SHARED
                    {
                        pair = Some((a, b));
                        evidence = Some("context");
                        break 'olds;
                    }
                }
            }
        }

        let make =
            |outcome: &str, ev: &str, a: &str, b: &str| -> Result<SupersessionMatch, DetectError> {
                let surface = surface_form(&old_body, b)?;
                Ok(SupersessionMatch {
                    memory_id: old.id.clone(),
                    outcome: unicode::cps(outcome),
                    evidence: unicode::cps(ev),
                    cue: if outcome == "supersedes" {
                        cues.first().cloned()
                    } else {
                        None
                    },
                    new_value: new_surface
                        .iter()
                        .find(|(token, _)| token == a)
                        .map_or_else(|| unicode::cps(a), |(_, form)| form.clone()),
                    old_value: surface.unwrap_or_else(|| unicode::cps(b)),
                    similarity: round4(similarity),
                    summary: models_text::snippet_for(&old_body, SUMMARY_CHARS),
                })
            };

        if !cues.is_empty() {
            if let (Some((a, b)), Some(ev)) = (pair, evidence) {
                let found = make("supersedes", ev, a, b)?;
                let lowered = unicode::lower(&found.old_value);
                if *replaced.get_or_insert_with(|| lowered.clone()) == lowered {
                    supersedes.push(found);
                    if supersedes.len() >= MAX_LINKS_PER_WRITE {
                        break;
                    }
                }
            }
            continue;
        }
        if similarity >= MIN_CONFLICT_JACCARD
            || (!kin.is_empty() && similarity >= MIN_CONFLICT_KIN_JACCARD)
        {
            let (a, b) = match pair {
                Some(p) => p,
                None => match (new_values.first(), old_values.first()) {
                    (Some(a), Some(b)) => (a, b),
                    _ => continue,
                },
            };
            conflicts.push(make("conflict", evidence.unwrap_or("similarity"), a, b)?);
            if conflicts.len() >= MAX_LINKS_PER_WRITE {
                break;
            }
        }
    }
    Ok(SupersessionReport {
        eligible: true,
        supersedes,
        conflicts,
    })
}
