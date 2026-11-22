//! `audit.py` 859-1491: the re-audit dedup (`is_duplicate_audit`), the
//! miss probe (`probe_for_miss` and its helpers) and the retrieval shield
//! (`_count_recent_retrievals`, `_retrieved_ids`).
//!
//! The ranker (`search.run_search`) is the trait [`ProbeSearch`] and the
//! caller's admission is the trait [`ProbeAdmission`]; the clock is an
//! argument. An event is a `PyValue` dict as the log reads it. Every
//! exception the reference raises on these paths comes back as a
//! [`ProbeError`] by its type (and the mode check's message).

use crate::datetime::{parse_event_ts, DateTime};
use crate::events::event_id_items;
use crate::lexicon::{strip_stopwords, tokenize, tokenize_unstemmed};
use crate::pyvalue::{repr_str, PyStr, PyValue};
use crate::records::Memory;
use crate::search::lane::relevance_label_v2;
use crate::unicode::{self, cps};

use super::report::{MissHit, MissReport, Verdict};
use super::{
    ACK_SURFACE, MIN_PROBE_CONTENT_TOKENS, RETRIEVAL_EVENT_KINDS, THRESHOLD_RULE_V1,
    TOP_HITS_RETAINED,
};

/// An exception the probe or the dedup raises, by type.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProbeError {
    /// The mode check's `ValueError`, by its message.
    ValueError(PyStr),
    /// `now - timedelta(seconds=...)` out of the years 1 to 9999.
    OverflowError,
    /// An unhashable value tested against a set (an event's `kind`, or
    /// its session in the retrieval shield).
    TypeError,
    /// `.get` on an event that is not a dict.
    AttributeError,
}

/// One hit as `run_search` returns it, reduced to what the report reads
/// (`matched_unique` is `len(match_terms)`).
#[derive(Debug, Clone, PartialEq)]
pub struct ProbeHit {
    pub id: PyStr,
    pub score: f64,
    pub relevance: PyStr,
    pub scopes: Vec<PyStr>,
    pub snippet: PyStr,
    pub matched_unique: i64,
    pub query_unique: i64,
}

/// The ranker's keyword arguments past the pool, the message and `now`.
/// The corpus statistics provider and the admission the reference also
/// forwards stay with the implementation.
#[derive(Debug, Clone, PartialEq)]
pub struct ProbeKnobs {
    pub mode: PyStr,
    pub excluded_scopes: Option<Vec<PyStr>>,
    pub max_results: usize,
    pub half_life_days: f64,
    pub conversational: bool,
    pub rescue_expansion: bool,
}

/// `run_search` as the probe calls it.
pub trait ProbeSearch {
    fn search(
        &mut self,
        pool: &[Memory],
        message: &[u32],
        now: &DateTime,
        knobs: &ProbeKnobs,
    ) -> Result<Vec<ProbeHit>, ProbeError>;
}

/// The two calls the probe makes on the caller's admission.
pub trait ProbeAdmission {
    fn prime(&mut self, ids: &[PyStr]);
    fn same_project(&mut self, id: &[u32]) -> bool;
}

/// The keyword arguments of `probe_for_miss` past `memories`,
/// `user_message`, `recent_events` and `admission`. `now` is required
/// (the reference reads the clock when it is None).
#[derive(Debug, Clone)]
pub struct ProbeArgs {
    pub session_id: PyStr,
    pub retrieval_session_id: Option<PyStr>,
    pub now: DateTime,
    pub lookback_seconds: i64,
    pub creation_shield_seconds: i64,
    pub worktree_root: Option<PyStr>,
    pub excluded_scopes: Option<Vec<PyStr>>,
    pub mode: PyStr,
    pub half_life_days: f64,
    pub conversational: bool,
    pub floor: bool,
    pub searched: bool,
}

impl ProbeArgs {
    /// The reference's defaults for a session and a clock.
    pub fn new(session_id: &[u32], now: DateTime) -> Self {
        ProbeArgs {
            session_id: session_id.to_vec(),
            retrieval_session_id: None,
            now,
            lookback_seconds: super::DEFAULT_LOOKBACK_SECONDS,
            creation_shield_seconds: super::DEFAULT_CREATION_SHIELD_SECONDS,
            worktree_root: None,
            excluded_scopes: None,
            mode: cps("hybrid"),
            half_life_days: 30.0,
            conversational: true,
            floor: true,
            searched: false,
        }
    }
}

/// `now - timedelta(seconds=seconds)`.
fn cutoff(now: &DateTime, seconds: i64) -> Result<DateTime, ProbeError> {
    now.minus_micros(i128::from(seconds) * 1_000_000)
        .map_err(|_| ProbeError::OverflowError)
}

/// `a < b` of two aware datetimes.
fn before(a: &DateTime, b: &DateTime) -> bool {
    a.instant_us() < b.instant_us()
}

/// `ts < cutoff` of an event's aware `ts`: TypeError when the cutoff is
/// naive (a naive `now` passed straight to the dedup or the shield).
fn older(ts: &DateTime, cut: &DateTime) -> Result<bool, ProbeError> {
    if cut.offset_us.is_none() {
        return Err(ProbeError::TypeError);
    }
    Ok(before(ts, cut))
}

static NONE: PyValue = PyValue::None;

fn get<'a>(ev: &'a PyValue, key: &str) -> Result<&'a PyValue, ProbeError> {
    match ev {
        PyValue::Dict(_) => Ok(ev.get(key).unwrap_or(&NONE)),
        _ => Err(ProbeError::AttributeError),
    }
}

/// `a or b`.
fn or<'a>(a: &'a PyValue, b: &'a PyValue) -> &'a PyValue {
    if crate::events::truthy(a) {
        a
    } else {
        b
    }
}

fn is_str(v: &PyValue, text: &[u32]) -> bool {
    matches!(v, PyValue::Str(s) if s == text)
}

/// `value in frozenset_of_str`: TypeError when `value` is unhashable.
fn in_set(v: &PyValue, set: &[PyStr]) -> Result<bool, ProbeError> {
    if !crate::response_trust::hashable(v) {
        return Err(ProbeError::TypeError);
    }
    Ok(set.iter().any(|s| is_str(v, s)))
}

fn ts_of(ev: &PyValue) -> Result<Option<DateTime>, ProbeError> {
    Ok(match get(ev, "ts")? {
        PyValue::Str(s) => parse_event_ts(s),
        _ => None,
    })
}

/// `is_duplicate_audit`.
pub fn is_duplicate_audit(
    events: &[PyValue],
    session_id: &[u32],
    probe_query_hash: Option<&[u32]>,
    probe_query_text: &[u32],
    now: &DateTime,
    window_seconds: i64,
) -> Result<bool, ProbeError> {
    let cutoff = cutoff(now, window_seconds)?;
    for ev in events.iter().rev() {
        if !is_str(get(ev, "kind")?, &cps("turn_audited")) {
            continue;
        }
        if !is_str(or(get(ev, "session_id")?, get(ev, "session")?), session_id) {
            continue;
        }
        let Some(ts) = ts_of(ev)? else {
            continue;
        };
        if older(&ts, &cutoff)? {
            break;
        }
        match get(ev, "probe_query")? {
            pq @ PyValue::Dict(_) => {
                if let Some(hash) = probe_query_hash {
                    if is_str(pq.get("hash").unwrap_or(&PyValue::None), hash) {
                        return Ok(true);
                    }
                }
            }
            PyValue::Str(pq) if pq == probe_query_text => return Ok(true),
            _ => {}
        }
    }
    Ok(false)
}

/// `probe_for_miss`.
pub fn probe_for_miss(
    memories: &[Memory],
    user_message: &[u32],
    recent_events: &[PyValue],
    admission: Option<&mut (dyn ProbeAdmission + '_)>,
    ranker: &mut dyn ProbeSearch,
    args: &ProbeArgs,
) -> Result<MissReport, ProbeError> {
    let now = args.now.assume_utc();
    let no_signal =
        |pq: Option<&[u32]>| no_signal_report(now, &args.session_id, args.lookback_seconds, pq);
    if unicode::py_strip(user_message).is_empty() || (memories.is_empty() && !args.searched) {
        return Ok(no_signal(None));
    }
    if args.floor && too_thin_to_probe(user_message) {
        return Ok(no_signal(Some(user_message)));
    }
    if memories.is_empty() {
        return Ok(no_signal(Some(user_message)));
    }
    let pool = created_before_shield(memories, &now, args.creation_shield_seconds)?;
    let hits = probe_hits(&pool, user_message, &now, args, ranker)?;
    if hits.is_empty() {
        return Ok(no_signal(Some(user_message)));
    }
    probe_report(
        miss_hits(&hits),
        user_message,
        recent_events,
        &now,
        args,
        admission,
    )
}

/// `_no_signal_report`.
pub fn no_signal_report(
    now: DateTime,
    session_id: &[u32],
    lookback_seconds: i64,
    probe_query: Option<&[u32]>,
) -> MissReport {
    MissReport {
        verdict: Verdict::NoSignal,
        checked_at: now,
        session_id: session_id.to_vec(),
        lookback_seconds,
        recent_retrieval_count: 0,
        threshold_rule: cps(THRESHOLD_RULE_V1),
        top_hits: Vec::new(),
        probe_query: probe_query.map(<[u32]>::to_vec),
        no_signal_reason: None,
        suppressed_by: None,
    }
}

/// `_ACK_TOKENS`: `_ACK_SURFACE` through the unstemmed tokenizer.
pub fn ack_tokens() -> Vec<String> {
    let mut out: Vec<String> = ACK_SURFACE
        .iter()
        .flat_map(|w| tokenize_unstemmed(&cps(w)))
        .collect();
    out.sort();
    out.dedup();
    out
}

/// `str.isdigit` of a token.
fn all_digits(t: &str) -> bool {
    !t.is_empty() && t.chars().all(|c| unicode::is_digit(u32::from(c)))
}

fn content_set(tokens: Vec<String>) -> Vec<String> {
    let mut out: Vec<String> = strip_stopwords(&tokens)
        .into_iter()
        .filter(|t| !all_digits(t))
        .collect();
    out.sort();
    out.dedup();
    out
}

/// `_too_thin_to_probe`.
pub fn too_thin_to_probe(user_message: &[u32]) -> bool {
    let content = content_set(tokenize(user_message));
    if content.len() < MIN_PROBE_CONTENT_TOKENS {
        return true;
    }
    let ack = ack_tokens();
    content_set(tokenize_unstemmed(user_message))
        .iter()
        .all(|t| ack.binary_search(t).is_ok())
}

/// `_created_before_shield`.
pub fn created_before_shield(
    memories: &[Memory],
    now: &DateTime,
    creation_shield_seconds: i64,
) -> Result<Vec<Memory>, ProbeError> {
    let cut = cutoff(now, creation_shield_seconds)?;
    Ok(memories
        .iter()
        .filter(|m| !before(&cut, &m.fields.created.assume_utc()))
        .cloned()
        .collect())
}

/// `_probe_hits`: the mode check, then the ranker.
pub fn probe_hits(
    pool: &[Memory],
    user_message: &[u32],
    now: &DateTime,
    args: &ProbeArgs,
    ranker: &mut dyn ProbeSearch,
) -> Result<Vec<ProbeHit>, ProbeError> {
    if !["keyword", "bm25", "hybrid"]
        .iter()
        .any(|m| cps(m) == args.mode)
    {
        let mut msg = cps("unknown audit probe mode ");
        msg.extend(repr_str(&args.mode));
        msg.extend(cps("; must be one of: keyword, bm25, hybrid"));
        return Err(ProbeError::ValueError(msg));
    }
    let knobs = ProbeKnobs {
        mode: args.mode.clone(),
        excluded_scopes: args.excluded_scopes.clone(),
        max_results: TOP_HITS_RETAINED,
        half_life_days: args.half_life_days,
        conversational: args.conversational,
        rescue_expansion: false,
    };
    ranker.search(pool, user_message, now, &knobs)
}

/// `_miss_hits`.
pub fn miss_hits(hits: &[ProbeHit]) -> Vec<MissHit> {
    hits.iter()
        .map(|h| MissHit {
            id: h.id.clone(),
            score: h.score,
            relevance: h.relevance.clone(),
            scopes: h.scopes.clone(),
            snippet: h.snippet.clone(),
            matched_unique: h.matched_unique,
            query_unique: h.query_unique,
            relevance_v2: cps(relevance_label_v2(h.matched_unique, h.query_unique)),
        })
        .collect()
}

fn probe_report(
    top_hits: Vec<MissHit>,
    user_message: &[u32],
    recent_events: &[PyValue],
    now: &DateTime,
    args: &ProbeArgs,
    mut admission: Option<&mut (dyn ProbeAdmission + '_)>,
) -> Result<MissReport, ProbeError> {
    let clears_threshold = top_hits[0].relevance == cps("high");
    let session_ids: Vec<PyStr> = [args.retrieval_session_id.as_ref(), Some(&args.session_id)]
        .into_iter()
        .flatten()
        .filter(|s| !s.is_empty())
        .cloned()
        .collect();
    let recent = count_recent_retrievals(
        recent_events,
        &session_ids,
        now,
        args.lookback_seconds,
        admission.as_deref_mut(),
        args.worktree_root.as_deref(),
    )?;
    let (verdict, suppressed_by) = probe_verdict(&top_hits, clears_threshold, recent, admission);
    Ok(MissReport {
        verdict,
        checked_at: *now,
        session_id: args.session_id.clone(),
        lookback_seconds: args.lookback_seconds,
        recent_retrieval_count: recent,
        threshold_rule: cps(THRESHOLD_RULE_V1),
        top_hits,
        probe_query: Some(user_message.to_vec()),
        no_signal_reason: None,
        suppressed_by: suppressed_by.map(cps),
    })
}

/// `_probe_verdict`.
pub fn probe_verdict(
    top_hits: &[MissHit],
    clears_threshold: bool,
    recent_retrieval_count: i64,
    admission: Option<&mut (dyn ProbeAdmission + '_)>,
) -> (Verdict, Option<&'static str>) {
    if !clears_threshold {
        (Verdict::Ok, None)
    } else if recent_retrieval_count > 0 {
        (Verdict::Ok, Some("retrieval"))
    } else if caller_in_top_hit_project(&top_hits[..top_hits.len().min(1)], admission) {
        (Verdict::Ok, Some("project_cohort"))
    } else {
        (Verdict::Miss, None)
    }
}

/// `_caller_in_top_hit_project`.
pub fn caller_in_top_hit_project(
    top_hits: &[MissHit],
    admission: Option<&mut (dyn ProbeAdmission + '_)>,
) -> bool {
    let Some(admission) = admission else {
        return false;
    };
    let prefix = cps("projects:");
    top_hits.iter().any(|hit| {
        hit.scopes.iter().any(|s| s.starts_with(&prefix)) && admission.same_project(&hit.id)
    })
}

/// `_count_recent_retrievals`. `session_ids` is the set the reference
/// tests membership in.
pub fn count_recent_retrievals(
    events: &[PyValue],
    session_ids: &[PyStr],
    now: &DateTime,
    lookback_seconds: i64,
    mut admission: Option<&mut (dyn ProbeAdmission + '_)>,
    worktree_root: Option<&[u32]>,
) -> Result<i64, ProbeError> {
    let cut = cutoff(now, lookback_seconds)?;
    let kinds: Vec<PyStr> = RETRIEVAL_EVENT_KINDS.iter().map(|k| cps(k)).collect();
    let mut window = Vec::new();
    for ev in events {
        if !in_set(get(ev, "kind")?, &kinds)? {
            continue;
        }
        if let Some(ts) = ts_of(ev)? {
            if !older(&ts, &cut)? {
                window.push(ev);
            }
        }
    }
    if let Some(a) = admission.as_deref_mut() {
        let ids: Vec<PyStr> = window.iter().flat_map(|ev| retrieved_ids(ev)).collect();
        a.prime(&ids);
    }
    let mut count = 0;
    for ev in window {
        if in_set(or(get(ev, "session")?, get(ev, "session_id")?), session_ids)? {
            count += 1;
            continue;
        }
        if let Some(root) = worktree_root {
            if is_str(get(ev, "worktree_root")?, root) {
                count += 1;
                continue;
            }
        }
        if let Some(a) = admission.as_deref_mut() {
            if retrieved_ids(ev).iter().any(|rid| a.same_project(rid)) {
                count += 1;
            }
        }
    }
    Ok(count)
}

/// `_retrieved_ids`: the ids under `returned`, `memory_ids`, `hit_ids`,
/// `shown_ids`, `ids` and `id`, in that order. A non-dict names none.
pub fn retrieved_ids(event: &PyValue) -> Vec<PyStr> {
    [
        "returned",
        "memory_ids",
        "hit_ids",
        "shown_ids",
        "ids",
        "id",
    ]
    .iter()
    .flat_map(|k| event_id_items(event.get(k).unwrap_or(&PyValue::None)))
    .map(|(_, rid)| rid)
    .collect()
}
