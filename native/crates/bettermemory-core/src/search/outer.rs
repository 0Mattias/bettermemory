//! Units search-2a and search-2b: `search.search` (reference lines
//! 1747-1909) and `search.rank` (1912-1961) around the ranker, with
//! `_filter_candidates` and `candidate_admitted` (717-814), `_browse_hits`
//! (1330-1358), `_ranked_hits` (1726-1744),
//! `search_steps._record_matched_legs` (58-79) and `search_lane`'s leg
//! vocabulary (536-556).
//!
//! The ranked corpus is the admitted candidates, in order: [`Corpus::new`]
//! over them, so the BM25 denominator and every document frequency are
//! the reference's candidate list's. Everything the reference reads of
//! the request or the machine comes in as an argument: the admission
//! ([`Admission`]), the anchoring, the drift read, the snippet memo, the
//! anchor's path checks and `hydrate` ([`Effects`]), and the clock (`now`:
//! the reference's default, the wall clock in UTC, is the caller's to
//! read).

use std::collections::{HashMap, HashSet};

use super::hit::{build_hit, claim_anchors, Drift, RecordAnchors, Scored};
use super::snippet::SnippetMemo;
use super::{query_tokens, slice_len, Corpus, Legs, Options, SearchError};
use crate::datetime::DateTime;
use crate::identity::{self, actor_matches};
use crate::pyvalue::PyStr;
use crate::records::summaries::MemoryHit;
use crate::records::{Actor, Memory};
use crate::unicode::{self, cps};
use crate::verify_anchoring::anchor::{AnchorPaths, ClaimAnchor, UNANCHORED};
use crate::verify_anchoring::detect::DriftError;
use crate::verify_anchoring::passes::ReadAnchor;

/// `search_lane.LEG_LEXICAL`.
pub const LEG_LEXICAL: &str = "lexical";
/// `search_lane.LEG_EXPANSION`.
pub const LEG_EXPANSION: &str = "expansion";

/// What an effect the caller hands in raised (an admission's read,
/// `hydrate`): the caller's own description, passed through untouched.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EffectError(pub String);

/// What escapes [`search`] and [`rank`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OuterError {
    /// The ranker's refusal or exception, and the ranked path's
    /// `zip(..., strict=True)` over a `hydrate` answer of another length
    /// (ValueError).
    Search(SearchError),
    /// A hit's drift read raised.
    Drift(DriftError),
    /// The admission or `hydrate` raised.
    Effect(EffectError),
}

impl From<SearchError> for OuterError {
    fn from(e: SearchError) -> Self {
        OuterError::Search(e)
    }
}

impl From<DriftError> for OuterError {
    fn from(e: DriftError) -> Self {
        OuterError::Drift(e)
    }
}

impl From<EffectError> for OuterError {
    fn from(e: EffectError) -> Self {
        OuterError::Effect(e)
    }
}

/// What `_filter_candidates` asks of the request's admission
/// (`admission.Admission`).
pub trait Admission {
    /// `prime(record_ids)`: every record's id, in order, once a search.
    fn prime(&mut self, record_ids: &[String]) -> Result<(), EffectError>;
    /// `home(record_id)`'s `recorded_project`; None for a record with no
    /// homes row.
    fn home(&mut self, record_id: &str) -> Result<Option<PyStr>, EffectError>;
    /// `admits_row(scopes, recorded)`.
    fn admits_row(
        &mut self,
        scopes: &[PyStr],
        recorded: Option<&[u32]>,
    ) -> Result<bool, EffectError>;
}

/// The request's selection: the admission (None admits every record, as
/// the offline callers rank) and the declared-writer filters.
#[derive(Default)]
pub struct Selection<'a> {
    pub admission: Option<&'a mut dyn Admission>,
    pub client: Option<&'a [u32]>,
    pub model: Option<&'a [u32]>,
}

/// `hydrate`: the records of the hits about to be built, in order, to the
/// records to build them from.
pub type Hydrate<'a> = &'a mut dyn FnMut(&[Memory]) -> Result<Vec<Memory>, EffectError>;

/// What building the hits reads: the anchoring (None: every record
/// unanchored, nothing asked), the anchor's path checks (`HostPaths` on a
/// host), the drift read, the snippet memo, `hydrate` and `path_drift`.
pub struct Effects<'a> {
    pub anchoring: Option<&'a mut dyn RecordAnchors>,
    pub paths: &'a dyn AnchorPaths,
    pub drift: &'a mut dyn Drift,
    pub memo: &'a SnippetMemo,
    pub hydrate: Option<Hydrate<'a>>,
    pub path_drift: bool,
}

/// `matched_leg_out`: a dict in insertion order, id to leg.
pub type LegsOut = Vec<(String, String)>;

/// `_matched_leg(lexical=, expansion=)`: lexical wins; "" when neither.
pub fn matched_leg(lexical: bool, expansion: bool) -> &'static str {
    if lexical {
        LEG_LEXICAL
    } else if expansion {
        LEG_EXPANSION
    } else {
        ""
    }
}

/// `_record_matched_legs`: each trimmed hit's leg, when it has one, set in
/// `out` (`out[id] = leg`: an id already there keeps its place).
pub fn record_matched_legs(trimmed: &[&str], legs: &Legs, out: &mut LegsOut) {
    let mut at: HashMap<String, usize> = out
        .iter()
        .enumerate()
        .map(|(i, (k, _))| (k.clone(), i))
        .collect();
    for &id in trimmed {
        let leg = matched_leg(legs.lexical.contains(id), legs.expansion.contains(id));
        if leg.is_empty() {
            continue;
        }
        match at.get(id) {
            Some(&i) => out[i].1 = leg.to_string(),
            None => {
                at.insert(id.to_string(), out.len());
                out.push((id.to_string(), leg.to_string()));
            }
        }
    }
}

/// The declared identity `identity.actor_matches` reads.
fn declared(actor: &Actor) -> identity::Actor {
    identity::Actor {
        client: actor.client.clone(),
        model: actor.model.clone(),
        ..identity::Actor::default()
    }
}

/// `candidate_admitted`: excluded scopes first, then the scope filter,
/// then the admission's `admits_row`, then the declared writer (exact and
/// case-sensitive; a record with no actor matches neither filter).
pub fn candidate_admitted(
    scopes: &[PyStr],
    home: Option<&[u32]>,
    actor: Option<&Actor>,
    scope_filter: Option<&HashSet<PyStr>>,
    excluded: &HashSet<PyStr>,
    sel: &mut Selection<'_>,
) -> Result<bool, EffectError> {
    if scopes.iter().any(|s| excluded.contains(s)) {
        return Ok(false);
    }
    if scope_filter.is_some_and(|f| !scopes.iter().any(|s| f.contains(s))) {
        return Ok(false);
    }
    if let Some(admission) = sel.admission.as_deref_mut() {
        if !admission.admits_row(scopes, home)? {
            return Ok(false);
        }
    }
    let actor = actor.map(declared);
    Ok(actor_matches(actor.as_ref(), sel.client, sel.model))
}

/// A record id as `str` (a validated id is ASCII).
fn id_text(id: &[u32]) -> String {
    id.iter().filter_map(|&c| char::from_u32(c)).collect()
}

/// `_filter_candidates`: the admitted records in order; the homes primed
/// once with every record's id, and each record's home read, when there
/// is an admission.
pub fn filter_candidates<'m>(
    memories: &'m [Memory],
    scopes: Option<&[String]>,
    excluded_scopes: Option<&[String]>,
    sel: &mut Selection<'_>,
) -> Result<Vec<&'m Memory>, EffectError> {
    let set = |v: &[String]| -> HashSet<PyStr> { v.iter().map(|s| cps(s)).collect() };
    let scope_filter = scopes.filter(|s| !s.is_empty()).map(set);
    let excluded = excluded_scopes.map(set).unwrap_or_default();
    if let Some(admission) = sel.admission.as_deref_mut() {
        let ids: Vec<String> = memories.iter().map(|m| id_text(&m.fields.id)).collect();
        admission.prime(&ids)?;
    }
    let mut out = Vec::new();
    for memory in memories {
        let f = &memory.fields;
        let home = match sel.admission.as_deref_mut() {
            Some(admission) => admission.home(&id_text(&f.id))?,
            None => None,
        };
        let scopes = &f.scopes;
        let actor = f.actor.as_ref();
        if candidate_admitted(
            scopes,
            home.as_deref(),
            actor,
            scope_filter.as_ref(),
            &excluded,
            sel,
        )? {
            out.push(memory);
        }
    }
    Ok(out)
}

/// A `ClaimAnchor` as the drift read reads it. With no translation the
/// path is its own target, as `ClaimAnchor.absolute` answers; a path a
/// translation must read and that holds a lone surrogate is unanchored.
struct Anchored<'a> {
    anchor: &'a ClaimAnchor,
    paths: &'a dyn AnchorPaths,
}

impl ReadAnchor for Anchored<'_> {
    fn root(&self) -> Option<PyStr> {
        self.anchor.root.as_deref().map(cps)
    }
    fn rule(&self) -> String {
        self.anchor.rule.clone()
    }
    fn convicting(&self) -> bool {
        self.anchor.convicting()
    }
    fn absolute(&self, path: &[u32]) -> (Option<PyStr>, String) {
        if self.anchor.translate.is_none() {
            return (Some(path.to_vec()), "absolute".to_string());
        }
        let Some(text) = unicode::to_string(path) else {
            return (None, UNANCHORED.to_string());
        };
        let (target, rule) = self.anchor.absolute(&text, self.paths);
        (target.map(|t| cps(&t)), rule)
    }
}

/// `_ranked_hits` and the hits of `_browse_hits`: the page's anchors read
/// first, then each hit built in order.
fn hits(
    records: &[Memory],
    scores: &[(f64, Vec<String>)],
    query_unique: usize,
    fx: &mut Effects<'_>,
) -> Result<Vec<MemoryHit>, OuterError> {
    let anchors = match &mut fx.anchoring {
        Some(a) => claim_anchors(Some(&mut **a), records),
        None => claim_anchors(None, records),
    };
    let mut out = Vec::with_capacity(records.len());
    for ((memory, (score, matched)), anchor) in records.iter().zip(scores).zip(&anchors) {
        let read = anchor.as_ref().map(|anchor| Anchored {
            anchor,
            paths: fx.paths,
        });
        let scored = Scored {
            memory,
            score: *score,
            matched,
            query_unique,
        };
        let read = read.as_ref().map(|r| r as &dyn ReadAnchor);
        out.push(build_hit(fx.memo, fx.drift, &scored, read, fx.path_drift)?);
    }
    Ok(out)
}

/// `updated`'s order as Python's: the instant of an aware value, the wall
/// clock of a naive one.
fn updated_key(m: &Memory) -> i128 {
    let u = &m.fields.updated;
    u.wall_us() - i128::from(u.offset_us.unwrap_or(0))
}

/// `_browse_hits`: newest `(updated, id)` first, sliced, the slice's
/// `hydrate` answer taken as it is, as zero-score hits with no terms.
/// Candidates mixing naive and aware `updated` are TypeError: the sort's
/// key leads with `updated`, so it always compares such a pair.
fn browse_hits(
    mut candidates: Vec<&Memory>,
    max_results: i64,
    fx: &mut Effects<'_>,
) -> Result<Vec<MemoryHit>, OuterError> {
    let naive = |m: &Memory| m.fields.updated.offset_us.is_none();
    if let Some(first) = candidates.first() {
        let n = naive(first);
        if candidates.iter().any(|m| naive(m) != n) {
            return Err(SearchError::TypeError.into());
        }
    }
    candidates.sort_by(|a, b| {
        updated_key(b)
            .cmp(&updated_key(a))
            .then_with(|| b.fields.id.cmp(&a.fields.id))
    });
    let n = slice_len(candidates.len(), max_results);
    let mut browsed: Vec<Memory> = candidates[..n].iter().map(|m| (*m).clone()).collect();
    if let Some(hydrate) = fx.hydrate.as_deref_mut() {
        if !browsed.is_empty() {
            browsed = hydrate(&browsed)?;
        }
    }
    let scores = vec![(0.0, Vec::new()); browsed.len()];
    hits(&browsed, &scores, 0, fx)
}

/// The admitted candidates as one corpus, in order.
fn corpus(candidates: &[&Memory]) -> Result<Corpus, SearchError> {
    let records = candidates
        .iter()
        .map(|m| m.to_search_record().ok_or(SearchError::InvalidInput))
        .collect::<Result<Vec<_>, _>>()?;
    Corpus::new(&records)
}

/// `search.search(memories, query, ...)`: `opts` as the ranker reads it,
/// `sel` the admission and writer filters, `fx` what building the hits
/// reads, `legs_out` the `matched_leg_out` dict. The mode is checked by
/// [`super::Mode::parse`] before.
pub fn search(
    memories: &[Memory],
    query: &[u32],
    now: &DateTime,
    opts: &Options<'_>,
    sel: &mut Selection<'_>,
    fx: &mut Effects<'_>,
    legs_out: Option<&mut LegsOut>,
) -> Result<Vec<MemoryHit>, OuterError> {
    let (tokens, _) = query_tokens(query, opts.allow_empty_query);
    if tokens.is_empty() && !opts.allow_empty_query {
        return Ok(Vec::new());
    }
    let candidates = filter_candidates(memories, opts.scopes, opts.excluded_scopes, sel)?;
    if tokens.is_empty() {
        return browse_hits(candidates, opts.max_results, fx);
    }
    if candidates.is_empty() {
        return Ok(Vec::new());
    }
    let query_unique = tokens.iter().collect::<HashSet<_>>().len();
    let inner = Options {
        scopes: None,
        excluded_scopes: None,
        ..*opts
    };
    let mut legs = Legs::default();
    let tracked = legs_out.is_some().then_some(&mut legs);
    let ranked = corpus(&candidates)?.search_legs(query, now, &inner, tracked)?;
    if let Some(out) = legs_out {
        let ids: Vec<&str> = ranked.iter().map(|h| h.id.as_str()).collect();
        record_matched_legs(&ids, &legs, out);
    }
    let mut records: Vec<Memory> = ranked.iter().map(|h| candidates[h.index].clone()).collect();
    if let Some(hydrate) = fx.hydrate.as_deref_mut() {
        if !records.is_empty() {
            let whole = hydrate(&records)?;
            if whole.len() != records.len() {
                return Err(SearchError::ValueError.into());
            }
            records = whole;
        }
    }
    let scores: Vec<(f64, Vec<String>)> = ranked
        .into_iter()
        .map(|h| (h.raw_score, h.match_terms))
        .collect();
    hits(&records, &scores, query_unique, fx)
}

/// `search.rank(memories, query, ...)`: each scored candidate's id and
/// match terms, in rank order, every one and no hit built. Of `opts` it
/// reads the mode, `half_life_days`, `rrf_k`, `conversational`,
/// `excluded_scopes` and the provider; as the reference, it filters by no
/// scope and no writer, runs no rescue leg and never browses.
pub fn rank(
    memories: &[Memory],
    query: &[u32],
    now: &DateTime,
    opts: &Options<'_>,
    admission: Option<&mut dyn Admission>,
) -> Result<Vec<(String, Vec<String>)>, OuterError> {
    let (tokens, _) = query_tokens(query, false);
    if tokens.is_empty() {
        return Ok(Vec::new());
    }
    let mut sel = Selection {
        admission,
        ..Selection::default()
    };
    let candidates = filter_candidates(memories, None, opts.excluded_scopes, &mut sel)?;
    if candidates.is_empty() {
        return Ok(Vec::new());
    }
    let inner = Options {
        max_results: i64::MAX,
        rescue_expansion: false,
        allow_empty_query: false,
        scopes: None,
        excluded_scopes: None,
        ..*opts
    };
    let ranked = corpus(&candidates)?.search(query, now, &inner)?;
    Ok(ranked.into_iter().map(|h| (h.id, h.match_terms)).collect())
}
