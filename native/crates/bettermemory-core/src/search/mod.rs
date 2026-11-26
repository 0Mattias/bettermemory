//! `search.search` over records already in memory: the keyword scorer, BM25
//! with the reference's statistics, their reciprocal-rank fusion, the
//! rescue-expansion leg, the conversational lane, the trim, and each hit's
//! score, relevance label and match terms.
//!
//! [`Corpus::new`] tokenizes each record once (the reference's token cache,
//! `search._memory_tokens`) and indexes its terms; [`Corpus::search`] ranks
//! a query. Every float is computed by the reference's operations in its
//! order: see `num` for the ones that are not a single IEEE-754 operation.
//!
//! Where the in-memory path stops: the reference's `admission` (membership
//! of each record's home), `client_filter`/`model_filter` (the record's
//! actor), `anchoring`, `hydrate`, `path_drift` and the hit's snippet and
//! drift report, and `matched_leg_out`, are not here; the store unit adds
//! them around [`Corpus::search`]. The corpus-stats provider is here, as a
//! callback ([`Options::corpus_stats_provider`]).

pub mod expansion;
pub mod hit;
pub mod lane;
pub mod num;
pub mod snippet;
pub mod token_cache;

use std::collections::{BTreeSet, HashMap, HashSet};

use crate::datetime::DateTime;
use crate::lexicon;
use crate::pyvalue::PyStr;

use lane::TemporalReading;
use num::{int_true_div, py_exp, py_log, py_max, round4};

/// Why a search was refused: the exception the reference raises, or
/// `InvalidInput` where the port refuses on its own (a record whose
/// fields name no datetime, a NaN half-life, candidates mixing naive and
/// aware `created`, or browse candidates mixing them in `updated`, where
/// the reference raises TypeError only if its sort compares such a pair).
/// TypeError is returned only where the reference always raises it: a
/// naive `now`, or a record naive in one of `created`/`updated` and aware
/// in the other, once it scores.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SearchError {
    ValueError,
    OverflowError,
    TypeError,
    InvalidInput,
}

impl SearchError {
    pub fn name(self) -> &'static str {
        match self {
            SearchError::ValueError => "ValueError",
            SearchError::OverflowError => "OverflowError",
            SearchError::TypeError => "TypeError",
            SearchError::InvalidInput => "InvalidInput",
        }
    }
}

/// A body's token stream, `_expand_kebab(tokenize(body))`: what the
/// scorers count against (`_MemoryTokens.body`).
pub fn body_stream(body: &[u32]) -> Vec<String> {
    lexicon::expand_kebab(&lexicon::tokenize(body))
}

/// `search_bm25._scope_tokens`: `_expand_kebab(tokenize(scope))`.
pub fn scope_tokens(scope: &str) -> Vec<String> {
    lexicon::expand_kebab(&lexicon::tokenize_str(scope))
}

/// The fields of a `Memory` ranking reads.
#[derive(Clone, Debug)]
pub struct Record {
    pub id: String,
    pub created: DateTime,
    pub updated: DateTime,
    pub scopes: Vec<String>,
    /// Code points: a body may hold a lone surrogate.
    pub body: PyStr,
}

/// `search(mode=...)`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Keyword,
    Bm25,
    Hybrid,
}

impl Mode {
    /// `search_steps._check_mode`: ValueError for anything else.
    pub fn parse(mode: &str) -> Result<Mode, SearchError> {
        match mode {
            "keyword" => Ok(Mode::Keyword),
            "bm25" => Ok(Mode::Bm25),
            "hybrid" => Ok(Mode::Hybrid),
            _ => Err(SearchError::ValueError),
        }
    }
}

/// `search_bm25.CorpusStats`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct CorpusStats {
    pub size: i64,
    pub body_df: HashMap<String, i64>,
    pub scope_df: HashMap<String, i64>,
    pub avgdl: Option<f64>,
}

/// A corpus-stats provider: given the terms, in the reference's order, the
/// statistics of the collection the search ranks.
pub type Provider<'a> = &'a dyn Fn(&[String]) -> Option<CorpusStats>;

/// `search`'s keyword arguments that the in-memory path reads.
#[derive(Clone, Copy)]
pub struct Options<'a> {
    pub mode: Mode,
    /// Python slice semantics: negative counts from the end.
    pub max_results: i64,
    pub half_life_days: f64,
    pub rrf_k: i64,
    pub conversational: bool,
    pub rescue_expansion: bool,
    pub allow_empty_query: bool,
    pub scopes: Option<&'a [String]>,
    pub excluded_scopes: Option<&'a [String]>,
    pub corpus_stats_provider: Option<Provider<'a>>,
}

impl Default for Options<'_> {
    /// The reference's defaults with `config.FIXED_SETTINGS`.
    fn default() -> Self {
        Options {
            mode: Mode::Hybrid,
            max_results: 5,
            half_life_days: 30.0,
            rrf_k: 60,
            conversational: true,
            rescue_expansion: false,
            allow_empty_query: false,
            scopes: None,
            excluded_scopes: None,
            corpus_stats_provider: None,
        }
    }
}

/// One ranked hit: the record (its index in the corpus), the unrounded
/// score, the score the hit carries (`round(score, 4)`), the relevance
/// label and the match terms.
#[derive(Clone, Debug, PartialEq)]
pub struct Hit {
    pub index: usize,
    pub id: String,
    pub raw_score: f64,
    pub score: f64,
    pub relevance: &'static str,
    pub match_terms: Vec<String>,
}

const BM25_K1: f64 = 1.2;
const BM25_B: f64 = 0.75;
const RESCUE_COVERAGE_GATE: f64 = 0.60;
const RESCUE_LEG_WEIGHT: f64 = 0.7;
const RESCUE_LEG_MIN_EVIDENCE: usize = 2;
const CONV_BAND_TAU: f64 = 0.50;
const CONV_WINDOW_BOOST: f64 = 0.30;
const CONV_WINDOW_DEMOTE: f64 = 0.0;
const FILLER_DF_FLOOR_RATIO: f64 = 0.5;
const SCAFFOLD_FLOOR_RATIO: f64 = 1.0;

struct Prepared {
    id: String,
    created_us: i128,
    updated_us: i128,
    /// `max(created, updated)`'s instant.
    fresh_us: i128,
    /// `created` is naive; `updated` is naive.
    created_naive: bool,
    updated_naive: bool,
    scopes: Vec<String>,
    /// The body stream's counts, by term id.
    body_tf: Vec<(u32, u32)>,
    /// The scope tokens, by term id, sorted.
    scope_set: Vec<u32>,
    /// The content stream's length.
    content_len: u64,
    anchor: i64,
}

impl Prepared {
    fn tf(&self, term: Option<u32>) -> u32 {
        let Some(t) = term else { return 0 };
        self.body_tf
            .binary_search_by_key(&t, |&(k, _)| k)
            .map_or(0, |i| self.body_tf[i].1)
    }
    fn in_scope(&self, term: Option<u32>) -> bool {
        term.is_some_and(|t| self.scope_set.binary_search(&t).is_ok())
    }
}

/// Records prepared for ranking.
pub struct Corpus {
    records: Vec<Prepared>,
    terms: HashMap<String, u32>,
    stop: Vec<bool>,
    body_post: Vec<Vec<u32>>,
    scope_post: Vec<Vec<u32>>,
    /// Per term over the whole corpus: records whose content stream holds
    /// it, and records whose content stream or scopes hold it.
    df_all: Vec<(i64, i64)>,
    total_content: u64,
}

/// A term of the query side: its text, its corpus id, its kebab parts.
struct QTerm {
    text: String,
    id: Option<u32>,
    parts: Vec<Option<u32>>,
    part_text: Vec<String>,
}

/// A leg's scored record: (record, score, matched term indices).
type Scored = (u32, f64, Vec<u32>);

struct Query<'c> {
    corpus: &'c Corpus,
    /// The term arena: the query's distinct tokens first, then any
    /// synthesized terms.
    terms: Vec<QTerm>,
    index: HashMap<String, u32>,
    now_us: Option<i128>,
    half_life: f64,
    mask: Option<Vec<bool>>,
    candidates: Vec<u32>,
}

fn slice_len(len: usize, max_results: i64) -> usize {
    if max_results >= 0 {
        len.min(usize::try_from(max_results).unwrap_or(usize::MAX))
    } else {
        let back = usize::try_from(max_results.unsigned_abs()).unwrap_or(usize::MAX);
        len.saturating_sub(back)
    }
}

fn instant_us(dt: &DateTime) -> i128 {
    dt.wall_us() - i128::from(dt.offset_us.unwrap_or(0))
}

/// `_relevance_label`.
fn relevance(matched: usize, query_unique: usize) -> &'static str {
    if query_unique == 0 {
        return "low";
    }
    let coverage = int_true_div(matched as i128, query_unique as i128);
    if coverage >= 0.75 {
        "high"
    } else if coverage >= 0.40 {
        "medium"
    } else {
        "low"
    }
}

/// Descending `(score, created, id)`, as the reference's sort keys read.
fn key_cmp(c: &Corpus, a: (u32, f64), b: (u32, f64)) -> std::cmp::Ordering {
    let (ra, rb) = (&c.records[a.0 as usize], &c.records[b.0 as usize]);
    b.1.partial_cmp(&a.1)
        .unwrap_or(std::cmp::Ordering::Equal)
        .then(rb.created_us.cmp(&ra.created_us))
        .then_with(|| rb.id.cmp(&ra.id))
}

impl Corpus {
    /// Tokenize and index `records`. InvalidInput for a timestamp whose
    /// fields name no datetime.
    pub fn new(records: &[Record]) -> Result<Corpus, SearchError> {
        let mut terms: HashMap<String, u32> = HashMap::new();
        let mut stop: Vec<bool> = Vec::new();
        let mut body_post: Vec<Vec<u32>> = Vec::new();
        let mut scope_post: Vec<Vec<u32>> = Vec::new();
        let intern = |t: &str,
                      terms: &mut HashMap<String, u32>,
                      stop: &mut Vec<bool>,
                      body_post: &mut Vec<Vec<u32>>,
                      scope_post: &mut Vec<Vec<u32>>|
         -> u32 {
            if let Some(&id) = terms.get(t) {
                return id;
            }
            let id = stop.len() as u32;
            terms.insert(t.to_owned(), id);
            stop.push(lexicon::is_stopword(t));
            body_post.push(Vec::new());
            scope_post.push(Vec::new());
            id
        };
        let mut prepared = Vec::with_capacity(records.len());
        let mut total_content = 0u64;
        for (ri, r) in records.iter().enumerate() {
            if !r.created.is_valid() || !r.updated.is_valid() {
                return Err(SearchError::InvalidInput);
            }
            let ri = u32::try_from(ri).map_err(|_| SearchError::InvalidInput)?;
            let body = body_stream(&r.body);
            let mut ids: Vec<u32> = Vec::with_capacity(body.len());
            let mut content_len = 0u64;
            for t in &body {
                let id = intern(t, &mut terms, &mut stop, &mut body_post, &mut scope_post);
                if !stop[id as usize] {
                    content_len += 1;
                }
                ids.push(id);
            }
            ids.sort_unstable();
            let mut body_tf: Vec<(u32, u32)> = Vec::new();
            for id in ids {
                match body_tf.last_mut() {
                    Some((last, n)) if *last == id => *n += 1,
                    _ => {
                        body_tf.push((id, 1));
                        body_post[id as usize].push(ri);
                    }
                }
            }
            let mut scope_set: Vec<u32> = Vec::new();
            for scope in &r.scopes {
                let toks = scope_tokens(scope);
                for t in &toks {
                    scope_set.push(intern(
                        t,
                        &mut terms,
                        &mut stop,
                        &mut body_post,
                        &mut scope_post,
                    ));
                }
            }
            scope_set.sort_unstable();
            scope_set.dedup();
            for &id in &scope_set {
                scope_post[id as usize].push(ri);
            }
            total_content += content_len;
            let created_us = instant_us(&r.created);
            let updated_us = instant_us(&r.updated);
            prepared.push(Prepared {
                id: r.id.clone(),
                created_us,
                updated_us,
                fresh_us: if updated_us > created_us {
                    updated_us
                } else {
                    created_us
                },
                created_naive: r.created.offset_us.is_none(),
                updated_naive: r.updated.offset_us.is_none(),
                scopes: r.scopes.clone(),
                body_tf,
                scope_set,
                content_len,
                anchor: lane::anchor_day(&r.body, &r.created),
            });
        }
        let df_all = (0..stop.len())
            .map(|t| {
                let (b, s) = (&body_post[t], &scope_post[t]);
                let union = merged_len(b, s);
                (b.len() as i64, union as i64)
            })
            .collect();
        Ok(Corpus {
            records: prepared,
            terms,
            stop,
            body_post,
            scope_post,
            df_all,
            total_content,
        })
    }

    pub fn len(&self) -> usize {
        self.records.len()
    }

    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }

    /// `search.search(records, query, now=now, ...)` with `path_drift`
    /// off: the hits, in order.
    pub fn search(
        &self,
        query: &[u32],
        now: &DateTime,
        opts: &Options<'_>,
    ) -> Result<Vec<Hit>, SearchError> {
        if !now.is_valid() || opts.half_life_days.is_nan() {
            return Err(SearchError::InvalidInput);
        }
        let raw = lexicon::tokenize(query);
        let mut query_tokens = lexicon::strip_stopwords(&raw);
        let mut stopword_fallback = false;
        if query_tokens.is_empty() && !raw.is_empty() && !opts.allow_empty_query {
            query_tokens = raw;
            stopword_fallback = true;
        }
        if query_tokens.is_empty() && !opts.allow_empty_query {
            return Ok(Vec::new());
        }
        let (candidates, mask) = self.filter(opts);
        if query_tokens.is_empty() {
            return self.browse(candidates, opts.max_results);
        }
        if candidates.is_empty() {
            return Ok(Vec::new());
        }
        // The reference compares `created` across records only where its
        // sorts happen to compare two tied scores, and raises TypeError
        // there if one is naive and the other aware; which pairs it
        // compares follows CPython's sort. This port refuses any such mix
        // among the candidates instead (InvalidInput): a divergence from
        // the reference, which a store never meets (`store_rows._dt` reads
        // every naive timestamp as UTC).
        let first_naive = self.records[candidates[0] as usize].created_naive;
        if candidates
            .iter()
            .any(|&i| self.records[i as usize].created_naive != first_naive)
        {
            return Err(SearchError::InvalidInput);
        }
        let mut q = Query {
            corpus: self,
            terms: Vec::new(),
            index: HashMap::new(),
            now_us: now.offset_us.map(|_| instant_us(now)),
            half_life: opts.half_life_days,
            mask,
            candidates,
        };
        for t in &query_tokens {
            q.term(t);
        }
        // The arena holds the query's tokens alone so far, each once, in
        // order of first occurrence: `dict.fromkeys(query_tokens)`.
        let uniq: Vec<u32> = (0..q.terms.len() as u32).collect();
        let query_unique = uniq.len();
        let scored = q.rank(query, &query_tokens, &uniq, now, opts, stopword_fallback)?;
        let n = slice_len(scored.len(), opts.max_results);
        Ok(scored[..n]
            .iter()
            .map(|(ri, score, matched)| Hit {
                index: *ri as usize,
                id: self.records[*ri as usize].id.clone(),
                raw_score: *score,
                score: round4(*score),
                relevance: relevance(matched.len(), query_unique),
                match_terms: matched
                    .iter()
                    .map(|&m| q.terms[m as usize].text.clone())
                    .collect(),
            })
            .collect())
    }

    /// `_filter_candidates` with no admission and no actor filter: the
    /// candidates in order, and their mask when they are not every record.
    fn filter(&self, opts: &Options<'_>) -> (Vec<u32>, Option<Vec<bool>>) {
        let set = |s: &'_ [String]| -> HashSet<String> { s.iter().cloned().collect() };
        let scope_filter = opts.scopes.filter(|s| !s.is_empty()).map(set);
        let excluded = opts.excluded_scopes.filter(|s| !s.is_empty()).map(set);
        if scope_filter.is_none() && excluded.is_none() {
            return ((0..self.records.len() as u32).collect(), None);
        }
        let mut mask = vec![false; self.records.len()];
        let mut out = Vec::new();
        for (i, r) in self.records.iter().enumerate() {
            if let Some(ex) = &excluded {
                if r.scopes.iter().any(|s| ex.contains(s)) {
                    continue;
                }
            }
            if let Some(f) = &scope_filter {
                if !r.scopes.iter().any(|s| f.contains(s)) {
                    continue;
                }
            }
            mask[i] = true;
            out.push(i as u32);
        }
        (out, Some(mask))
    }

    /// `_browse_hits`: newest `updated` first, as zero-score hits.
    fn browse(&self, mut candidates: Vec<u32>, max_results: i64) -> Result<Vec<Hit>, SearchError> {
        if let Some(&first) = candidates.first() {
            let naive = self.records[first as usize].updated_naive;
            if candidates
                .iter()
                .any(|&i| self.records[i as usize].updated_naive != naive)
            {
                // As above, for the browse sort's `updated`.
                return Err(SearchError::InvalidInput);
            }
        }
        candidates.sort_by(|&a, &b| {
            let (ra, rb) = (&self.records[a as usize], &self.records[b as usize]);
            rb.updated_us
                .cmp(&ra.updated_us)
                .then_with(|| rb.id.cmp(&ra.id))
        });
        let n = slice_len(candidates.len(), max_results);
        Ok(candidates[..n]
            .iter()
            .map(|&i| Hit {
                index: i as usize,
                id: self.records[i as usize].id.clone(),
                raw_score: 0.0,
                score: 0.0,
                relevance: "low",
                match_terms: Vec::new(),
            })
            .collect())
    }
}

/// The size of the union of two sorted lists.
fn merged_len(a: &[u32], b: &[u32]) -> usize {
    let (mut i, mut j, mut n) = (0, 0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                i += 1;
                j += 1;
            }
        }
        n += 1;
    }
    n + (a.len() - i) + (b.len() - j)
}

/// `max(1, int(size * ratio))`.
fn floor_of(size: i64, ratio: f64) -> i64 {
    let v = size as f64 * ratio;
    // `int()` of a finite float; sizes here are far inside i64.
    (v.trunc() as i64).max(1)
}

/// `_filler_floor_stats` (`keep_avgdl`) and `_scaffold_floor_stats`.
fn floor_stats(
    base: Option<CorpusStats>,
    present: &[&str],
    pool_n: i64,
    ratio: f64,
    keep_avgdl: bool,
) -> Option<CorpusStats> {
    if present.is_empty() {
        return base;
    }
    let size = base.as_ref().map_or(pool_n, |b| b.size);
    if size <= 0 {
        return base;
    }
    let floor = floor_of(size, ratio);
    let (mut body_df, mut scope_df, avgdl) = match base {
        Some(b) => (b.body_df, b.scope_df, b.avgdl),
        None => (HashMap::new(), HashMap::new(), None),
    };
    for t in present {
        let e = body_df.entry((*t).to_owned()).or_insert(0);
        *e = (*e).max(floor);
        let e = scope_df.entry((*t).to_owned()).or_insert(0);
        *e = (*e).max(floor);
    }
    Some(CorpusStats {
        size,
        body_df,
        scope_df,
        avgdl: if keep_avgdl { avgdl } else { None },
    })
}

/// `_merge_corpus_stats(a, b)`.
fn merge_stats(a: Option<CorpusStats>, b: Option<CorpusStats>) -> Option<CorpusStats> {
    match (a, b) {
        (None, b) => b,
        (a, None) => a,
        (Some(a), Some(b)) => {
            let mut body_df = a.body_df;
            body_df.extend(b.body_df);
            let mut scope_df = a.scope_df;
            scope_df.extend(b.scope_df);
            Some(CorpusStats {
                size: if b.size > 0 { b.size } else { a.size },
                body_df,
                scope_df,
                avgdl: b.avgdl.or(a.avgdl),
            })
        }
    }
}

/// The terms the provider is asked for: each term, then the kebab parts of
/// each, first occurrence kept.
fn fetch_terms(tokens: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    let mut push = |t: String, out: &mut Vec<String>| {
        if seen.insert(t.clone()) {
            out.push(t);
        }
    };
    for t in tokens {
        push(t.clone(), &mut out);
    }
    for t in tokens {
        for p in lexicon::kebab_parts(t) {
            push(p, &mut out);
        }
    }
    out
}

/// Okapi IDF, `log((n - df + 0.5) / (df + 0.5) + 1.0)`.
fn okapi(n: i64, df: i64) -> Result<f64, SearchError> {
    py_log(((n - df) as f64 + 0.5) / (df as f64 + 0.5) + 1.0)
}

impl Query<'_> {
    /// The arena index of `text`, added when new.
    fn term(&mut self, text: &str) -> u32 {
        if let Some(&i) = self.index.get(text) {
            return i;
        }
        let parts = lexicon::kebab_parts(text);
        let i = self.terms.len() as u32;
        self.terms.push(QTerm {
            text: text.to_owned(),
            id: self.corpus.terms.get(text).copied(),
            parts: parts
                .iter()
                .map(|p| self.corpus.terms.get(p).copied())
                .collect(),
            part_text: parts,
        });
        self.index.insert(text.to_owned(), i);
        i
    }

    fn admitted(&self, r: u32) -> bool {
        self.mask.as_ref().is_none_or(|m| m[r as usize])
    }

    /// The candidates any of `terms` (or their parts) can touch, in order.
    fn touched(&self, terms: &[u32]) -> Vec<u32> {
        let c = self.corpus;
        let mut seen = vec![false; c.records.len()];
        let mut ids: Vec<u32> = Vec::new();
        for &t in terms {
            let q = &self.terms[t as usize];
            for id in std::iter::once(q.id)
                .chain(q.parts.iter().copied())
                .flatten()
            {
                ids.push(id);
            }
        }
        let mut out = Vec::new();
        for id in ids {
            for list in [&c.body_post[id as usize], &c.scope_post[id as usize]] {
                for &r in list {
                    if !seen[r as usize] && self.admitted(r) {
                        seen[r as usize] = true;
                        out.push(r);
                    }
                }
            }
        }
        out.sort_unstable();
        out
    }

    /// `_recency_factor(max(created, updated), now, half_life_days)`.
    fn recency(&self, r: u32) -> Result<f64, SearchError> {
        let now = self.now_us.ok_or(SearchError::TypeError)?;
        let rec = &self.corpus.records[r as usize];
        if rec.created_naive != rec.updated_naive {
            // `max(created, updated)` of a naive and an aware value.
            return Err(SearchError::TypeError);
        }
        let fresh = rec.fresh_us;
        let age_seconds = py_max(0.0, int_true_div(now - fresh, 1_000_000));
        let age_days = age_seconds / 86400.0;
        Ok(1.0 + 0.1 * py_exp(-age_days / py_max(self.half_life, 0.001))?)
    }

    /// `_score_keyword` with the stock scorer (`score_memory`).
    fn keyword_leg(&self, uniq: &[u32]) -> Result<Vec<Scored>, SearchError> {
        let query_unique = uniq.len();
        let mut out = Vec::new();
        for r in self.touched(uniq) {
            let rec = &self.corpus.records[r as usize];
            let mut raw = 0.0f64;
            let mut matched = Vec::new();
            for &t in uniq {
                let q = &self.terms[t as usize];
                let mut body_hits = rec.tf(q.id);
                let mut scope_hit = u32::from(rec.in_scope(q.id));
                if body_hits == 0 && scope_hit == 0 && !q.parts.is_empty() {
                    let min = q.parts.iter().map(|&p| rec.tf(p)).min().unwrap_or(0);
                    if min > 0 {
                        body_hits = min;
                    }
                    if q.parts.iter().all(|&p| rec.in_scope(p)) {
                        scope_hit = 1;
                    }
                }
                let mut contrib = f64::from(body_hits.min(2) + 2 * scope_hit);
                if query_unique == 1 && body_hits > 2 {
                    contrib += f64::from(body_hits - 2).ln_1p();
                }
                if contrib > 0.0 {
                    matched.push(t);
                }
                raw += contrib;
            }
            if raw == 0.0 {
                continue;
            }
            let coverage = int_true_div(matched.len() as i128, query_unique as i128);
            let base = raw * (0.5 + 0.5 * coverage);
            let score = base * self.recency(r)?;
            if score > 0.0 {
                out.push((r, score, matched));
            }
        }
        Ok(out)
    }

    /// `compute_idf(candidates, terms=idf_terms(tokens))`: the body and
    /// scope IDF of each wanted term (None where the map has no entry),
    /// and avgdl.
    #[allow(clippy::type_complexity)]
    fn idf(
        &self,
        uniq: &[u32],
        stats: Option<&CorpusStats>,
    ) -> Result<(HashMap<String, (Option<f64>, Option<f64>)>, f64), SearchError> {
        let c = self.corpus;
        let n = self.candidates.len() as i64;
        let mut wanted: Vec<(&str, Option<u32>)> = Vec::new();
        let mut seen: HashSet<&str> = HashSet::new();
        for &t in uniq {
            let q = &self.terms[t as usize];
            for (text, id) in std::iter::once((q.text.as_str(), q.id)).chain(
                q.part_text
                    .iter()
                    .map(String::as_str)
                    .zip(q.parts.iter().copied()),
            ) {
                if seen.insert(text) {
                    wanted.push((text, id));
                }
            }
        }
        let total_len: u64 = match &self.mask {
            None => c.total_content,
            Some(_) => self
                .candidates
                .iter()
                .map(|&r| c.records[r as usize].content_len)
                .sum(),
        };
        let mut avgdl = int_true_div(i128::from(total_len), i128::from(n));
        if let Some(s) = stats {
            if s.size > 0 {
                if let Some(a) = s.avgdl {
                    if a > 0.0 {
                        avgdl = a;
                    }
                }
            }
        }
        let mut maps = HashMap::new();
        for (text, id) in wanted {
            let (body_df, scope_df) = match id {
                None => (0, 0),
                Some(id) => {
                    let stop = c.stop[id as usize];
                    let (body_n, union_n) = match &self.mask {
                        None => c.df_all[id as usize],
                        Some(m) => {
                            let b = &c.body_post[id as usize];
                            let s = &c.scope_post[id as usize];
                            let bn = b.iter().filter(|&&r| m[r as usize]).count() as i64;
                            let un = bn
                                + s.iter()
                                    .filter(|&&r| m[r as usize] && b.binary_search(&r).is_err())
                                    .count() as i64;
                            (bn, un)
                        }
                    };
                    if stop {
                        let sn = match &self.mask {
                            None => c.scope_post[id as usize].len() as i64,
                            Some(m) => c.scope_post[id as usize]
                                .iter()
                                .filter(|&&r| m[r as usize])
                                .count() as i64,
                        };
                        (0, sn)
                    } else {
                        (body_n, union_n)
                    }
                }
            };
            let mut body = if body_df > 0 {
                Some(okapi(n, body_df)?)
            } else {
                None
            };
            let mut scope = if scope_df > 0 {
                Some(okapi(n, scope_df)?)
            } else {
                None
            };
            if let Some(s) = stats {
                if s.size > 0 {
                    if let (Some(_), Some(&dfi)) = (body, s.body_df.get(text)) {
                        if dfi > 0 {
                            body = Some(okapi(s.size, dfi)?);
                        }
                    }
                    if let (Some(_), Some(&dfi)) = (scope, s.scope_df.get(text)) {
                        if dfi > 0 {
                            scope = Some(okapi(s.size, dfi)?);
                        }
                    }
                }
            }
            maps.insert(text.to_owned(), (body, scope));
        }
        Ok((maps, avgdl))
    }

    /// `_score_bm25`.
    fn bm25_leg(
        &self,
        uniq: &[u32],
        fallback: bool,
        stats: Option<&CorpusStats>,
    ) -> Result<Vec<Scored>, SearchError> {
        let (maps, avgdl) = self.idf(uniq, stats)?;
        if avgdl <= 0.0 || avgdl.is_nan() {
            return Ok(Vec::new());
        }
        let k1p1 = BM25_K1 + 1.0;
        let body_idf = |t: &str, default: f64| maps.get(t).and_then(|m| m.0).unwrap_or(default);
        let scope_idf = |t: &str, default: f64| maps.get(t).and_then(|m| m.1).unwrap_or(default);
        // Each term's prices, read once: (body, scope, the conjunctive
        // fallback's body and scope), the values the per-record lookups
        // would read.
        let prices: Vec<(f64, f64, f64, f64)> = uniq
            .iter()
            .map(|&t| {
                let q = &self.terms[t as usize];
                let content: Vec<&String> = q
                    .part_text
                    .iter()
                    .filter(|p| !lexicon::is_stopword(p))
                    .collect();
                let part_body = if content.is_empty() {
                    1.0
                } else {
                    content
                        .iter()
                        .map(|p| body_idf(p, 0.0))
                        .fold(f64::INFINITY, py_min)
                };
                let part_scope = q
                    .part_text
                    .iter()
                    .map(|p| scope_idf(p, 1.0))
                    .fold(f64::INFINITY, py_min);
                (
                    body_idf(&q.text, if fallback { 1.0 } else { 0.0 }),
                    scope_idf(&q.text, 1.0),
                    part_body,
                    part_scope,
                )
            })
            .collect();
        let mut out = Vec::new();
        for r in self.touched(uniq) {
            let rec = &self.corpus.records[r as usize];
            let dl = rec.content_len;
            let length_norm = (1.0 - BM25_B) + BM25_B * (dl as f64 / avgdl);
            let mut score = 0.0f64;
            let mut matched = Vec::new();
            for (k, &t) in uniq.iter().enumerate() {
                let q = &self.terms[t as usize];
                let (direct_body, direct_scope, part_body, part_scope) = prices[k];
                let mut contrib = 0.0f64;
                let mut tf = rec.tf(q.id);
                if !fallback && tf > 0 && q.id.is_some_and(|id| self.corpus.stop[id as usize]) {
                    tf = 0;
                }
                let mut b_idf = direct_body;
                let mut scope_hit = rec.in_scope(q.id);
                let mut s_idf = direct_scope;
                if tf == 0 && !scope_hit && !q.parts.is_empty() {
                    let min = q.parts.iter().map(|&p| rec.tf(p)).min().unwrap_or(0);
                    if min > 0 {
                        tf = min;
                        b_idf = part_body;
                    }
                    if q.parts.iter().all(|&p| rec.in_scope(p)) {
                        scope_hit = true;
                        s_idf = part_scope;
                    }
                }
                if tf > 0 {
                    let denom = f64::from(tf) + BM25_K1 * length_norm;
                    contrib += if denom > 0.0 {
                        b_idf * f64::from(tf) * k1p1 / denom
                    } else {
                        0.0
                    };
                }
                if scope_hit {
                    contrib += 2.0 * s_idf;
                }
                if contrib > 0.0 {
                    matched.push(t);
                }
                score += contrib;
            }
            if score <= 0.0 || score.is_nan() {
                continue;
            }
            let score = score * self.recency(r)?;
            if score > 0.0 {
                out.push((r, score, matched));
            }
        }
        Ok(out)
    }

    fn sorted_ids(&self, leg: &[Scored]) -> Vec<u32> {
        let mut order: Vec<usize> = (0..leg.len()).collect();
        order.sort_by(|&a, &b| key_cmp(self.corpus, (leg[a].0, leg[a].1), (leg[b].0, leg[b].1)));
        order.into_iter().map(|i| leg[i].0).collect()
    }

    /// `_hybrid_fuse(legs, rrf_k, weights)`.
    fn fuse(
        &self,
        legs: &[&[Scored]],
        k: i64,
        weights: &[f64],
    ) -> Result<Vec<Scored>, SearchError> {
        if k <= 0 {
            return Err(SearchError::ValueError);
        }
        let c = self.corpus;
        let mut by_id: HashMap<&str, (u32, BTreeSet<&str>)> = HashMap::new();
        let mut lists = Vec::with_capacity(legs.len());
        for leg in legs {
            lists.push(self.sorted_ids(leg));
            for (r, _, matched) in leg.iter() {
                let entry = by_id
                    .entry(c.records[*r as usize].id.as_str())
                    .or_insert((*r, BTreeSet::new()));
                entry.1.extend(
                    matched
                        .iter()
                        .map(|&m| self.terms[m as usize].text.as_str()),
                );
            }
        }
        let mut fused: Vec<(&str, f64)> = Vec::new();
        let mut at: HashMap<&str, usize> = HashMap::new();
        for (i, list) in lists.iter().enumerate() {
            let w = weights[i];
            let mut seen: HashSet<&str> = HashSet::new();
            for (rank, &r) in list.iter().enumerate() {
                let id = c.records[r as usize].id.as_str();
                if !seen.insert(id) {
                    continue;
                }
                let add = w / (i128::from(k) + rank as i128 + 1) as f64;
                match at.get(id) {
                    Some(&j) => fused[j].1 += add,
                    None => {
                        at.insert(id, fused.len());
                        fused.push((id, 0.0 + add));
                    }
                }
            }
        }
        let mut out: Vec<Scored> = fused
            .iter()
            .map(|(id, s)| {
                let (r, matched) = &by_id[id];
                let m = matched.iter().map(|t| self.index[*t]).collect();
                (*r, *s, m)
            })
            .collect();
        out.sort_by(|a, b| key_cmp(c, (a.0, a.1), (b.0, b.1)));
        Ok(out)
    }

    #[allow(clippy::too_many_arguments)]
    fn rank(
        &mut self,
        query: &[u32],
        query_tokens: &[String],
        uniq: &[u32],
        now: &DateTime,
        opts: &Options<'_>,
        fallback: bool,
    ) -> Result<Vec<Scored>, SearchError> {
        // `_conversational_gates`.
        let mut reading: Option<TemporalReading> = None;
        if opts.conversational && opts.mode == Mode::Hybrid && !fallback {
            let r = lane::temporal_reading(query, now)?;
            if r.is_temporal() {
                reading = Some(r);
            }
        }
        let conv_pricing = reading.is_some();
        // `_fetch_corpus_stats`.
        let mut corpus_stats = None;
        if let Some(provider) = opts.corpus_stats_provider {
            if opts.mode != Mode::Keyword {
                corpus_stats = provider(&fetch_terms(query_tokens));
            }
        }
        let mut scored = match opts.mode {
            Mode::Keyword => {
                let mut s = self.keyword_leg(uniq)?;
                s.sort_by(|a, b| key_cmp(self.corpus, (a.0, a.1), (b.0, b.1)));
                s
            }
            Mode::Bm25 => {
                let mut s = self.bm25_leg(uniq, fallback, corpus_stats.as_ref())?;
                s.sort_by(|a, b| key_cmp(self.corpus, (a.0, a.1), (b.0, b.1)));
                s
            }
            Mode::Hybrid => {
                let pool_n = self.candidates.len() as i64;
                let mut hybrid_stats = corpus_stats;
                if opts.rescue_expansion {
                    let filler = &expansion::tables().filler_stems;
                    let present: Vec<&str> = query_tokens
                        .iter()
                        .filter(|t| filler.contains(*t))
                        .map(String::as_str)
                        .collect();
                    hybrid_stats =
                        floor_stats(hybrid_stats, &present, pool_n, FILLER_DF_FLOOR_RATIO, true);
                }
                if conv_pricing {
                    let present = lane::scaffold_terms(query_tokens);
                    hybrid_stats =
                        floor_stats(hybrid_stats, &present, pool_n, SCAFFOLD_FLOOR_RATIO, false);
                }
                let kw = self.keyword_leg(uniq)?;
                let bm = self.bm25_leg(uniq, fallback, hybrid_stats.as_ref())?;
                let mut scored = self.fuse(&[&kw, &bm], opts.rrf_k, &[1.0, 1.0])?;
                if opts.rescue_expansion && !fallback {
                    scored = self.rescue(scored, &kw, &bm, uniq, hybrid_stats, opts)?;
                }
                scored
            }
        };
        if let Some(reading) = reading {
            scored = self.rerank(scored, &reading);
        }
        Ok(scored)
    }

    /// `_rescue_fuse`.
    #[allow(clippy::too_many_arguments)]
    fn rescue(
        &mut self,
        scored: Vec<Scored>,
        kw: &[Scored],
        bm: &[Scored],
        uniq: &[u32],
        hybrid_stats: Option<CorpusStats>,
        opts: &Options<'_>,
    ) -> Result<Vec<Scored>, SearchError> {
        let coverage = match scored.first() {
            Some((_, _, matched)) => {
                let top = matched
                    .iter()
                    .filter(|&&m| (m as usize) < uniq.len())
                    .count();
                int_true_div(top as i128, uniq.len() as i128)
            }
            None => 0.0,
        };
        if coverage >= RESCUE_COVERAGE_GATE {
            return Ok(scored);
        }
        let distinct: Vec<String> = uniq
            .iter()
            .map(|&t| self.terms[t as usize].text.clone())
            .collect();
        let exp_terms = expansion::expansion_terms(&distinct);
        if exp_terms.is_empty() {
            return Ok(scored);
        }
        let exp_stats = match opts.corpus_stats_provider {
            Some(provider) => merge_stats(hybrid_stats, provider(&fetch_terms(&exp_terms))),
            None => hybrid_stats,
        };
        let exp_ids: Vec<u32> = exp_terms.iter().map(|t| self.term(t)).collect();
        let leg = self.bm25_leg(&exp_ids, false, exp_stats.as_ref())?;
        // `_leg_top_evidence`: the first maximal (score, created, id).
        let mut top: Option<&Scored> = None;
        for s in &leg {
            match top {
                Some(t) if key_cmp(self.corpus, (s.0, s.1), (t.0, t.1)).is_lt() => top = Some(s),
                None => top = Some(s),
                _ => {}
            }
        }
        let evidence = top.map_or(0, |t| t.2.len());
        if leg.is_empty() || evidence < RESCUE_LEG_MIN_EVIDENCE {
            return Ok(scored);
        }
        let fused = self.fuse(&[kw, bm, &leg], opts.rrf_k, &[1.0, 1.0, RESCUE_LEG_WEIGHT])?;
        Ok(fused
            .into_iter()
            .map(|(r, s, m)| {
                // The query's own tokens are the arena's first entries.
                let own = m.into_iter().filter(|&t| (t as usize) < uniq.len());
                (r, s, own.collect())
            })
            .collect())
    }

    /// `_conversational_rerank`.
    fn rerank(&self, scored: Vec<Scored>, reading: &TemporalReading) -> Vec<Scored> {
        let Some(top) = scored.first().map(|s| s.1) else {
            return scored;
        };
        if top <= 0.0 {
            return scored;
        }
        let threshold = CONV_BAND_TAU * top;
        let banded: Vec<usize> = (0..scored.len())
            .filter(|&i| scored[i].1 >= threshold)
            .collect();
        if banded.len() <= 1 {
            return scored;
        }
        // The selector arm's boost is 0.0 (`_CONV_SELECTOR_BOOST`), so its
        // factor is 1.0 + 0.0 * 0.7**k = 1.0 exactly for every finite
        // power: the scores stand and the re-sort, under the key the list
        // is already sorted by, moves nothing.
        let Some((lo, hi)) = reading.window else {
            return scored;
        };
        let mut adjusted = scored;
        for &i in &banded {
            let anchor = self.corpus.records[adjusted[i].0 as usize].anchor;
            let factor = if lo <= anchor && anchor <= hi {
                1.0 + CONV_WINDOW_BOOST
            } else {
                1.0 - CONV_WINDOW_DEMOTE
            };
            adjusted[i].1 *= factor;
        }
        adjusted.sort_by(|a, b| key_cmp(self.corpus, (a.0, a.1), (b.0, b.1)));
        adjusted
    }
}

/// Python's `min` of floats as a fold: the first of the least.
fn py_min(acc: f64, x: f64) -> f64 {
    if x < acc {
        x
    } else {
        acc
    }
}

/// `search.search(records, query, now=now, ...)`: prepares the corpus and
/// ranks one query.
pub fn search(
    records: &[Record],
    query: &[u32],
    now: &DateTime,
    opts: &Options<'_>,
) -> Result<Vec<Hit>, SearchError> {
    Corpus::new(records)?.search(query, now, opts)
}
