//! `eval_rates.py` whole: the steps of `eval.compute_eval`.
//!
//! [`walk_eval_events`] walks the event log once ([`count_search`] and
//! [`count_use`] for the retrieval and use rows),
//! [`resolve_audit_telemetry`] resolves the buffered audit telemetry
//! against the invalidation markers, and [`cold_endorsement_rows`] lists
//! the cold-endorsement rows. Nothing here asks the machine: the events,
//! the window bounds, the scope test (`passes_scope`, a callback the
//! caller owns), the tombstoned ids and the memories are arguments.
//!
//! The per-memory counts are the reference's dicts with their insertion
//! order ([`Counts`]), and `by_model` keeps its first-seen order: the
//! cold rows are sorted in the end, but a sort tie keeps the dict's order,
//! and the report prints `by_model` as built. The review and surface rows
//! and the buffered audit events are the events themselves, by reference,
//! as the reference keeps them.

use std::collections::{HashMap, HashSet, VecDeque};

use crate::datetime::{parse_event_ts, DateTime};
use crate::eval_model::{
    canonical_top_hit_id, silent_miss_from_event, ColdEndorsementMemoriesRow, EvalModelError,
    SilentMissCandidate,
};
use crate::events::truthy;
use crate::health_model::{
    endorsement_tier, is_hook_telemetry_event, weakly_endorsed, AppliedTier, HealthModelError,
};
use crate::models_text::first_summary_line;
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::recall_counters::is_surface_row;
use crate::recall_state::is_settlement;
use crate::records::{Category, Memory};
use crate::review_counters::{is_review_row, CountersError};
use crate::unicode::{cps, py_strip};

/// The exception a step of this module raises, by the module it comes
/// from. Over a list of dicts only two are reachable: TypeError from
/// `is_surface_row` (an unhashable kind) and from `is_settlement` (an
/// unhashable surface on an applied `use`), and TypeError for a naive
/// window bound met by an event's aware stamp.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EvalRatesError {
    Py(PyError),
    Health(HealthModelError),
    Counters(CountersError),
    Model(EvalModelError),
}

impl EvalRatesError {
    /// The exception's class name.
    pub fn name(&self) -> &'static str {
        match self {
            EvalRatesError::Py(e) => e.name(),
            EvalRatesError::Health(e) => e.name(),
            EvalRatesError::Counters(e) => e.name(),
            EvalRatesError::Model(e) => e.name(),
        }
    }
}

impl From<PyError> for EvalRatesError {
    fn from(e: PyError) -> Self {
        EvalRatesError::Py(e)
    }
}

impl From<HealthModelError> for EvalRatesError {
    fn from(e: HealthModelError) -> Self {
        EvalRatesError::Health(e)
    }
}

impl From<CountersError> for EvalRatesError {
    fn from(e: CountersError) -> Self {
        EvalRatesError::Counters(e)
    }
}

impl From<EvalModelError> for EvalRatesError {
    fn from(e: EvalModelError) -> Self {
        EvalRatesError::Model(e)
    }
}

type Answer<T> = Result<T, EvalRatesError>;

/// A `dict[str, int]` of per-memory counts, in insertion order.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Counts {
    items: Vec<(PyStr, i64)>,
    index: HashMap<PyStr, usize>,
}

impl Counts {
    /// `d[key] = d.get(key, 0) + 1`.
    fn bump(&mut self, key: &[u32]) {
        match self.index.get(key) {
            Some(&i) => self.items[i].1 += 1,
            None => {
                self.index.insert(key.to_vec(), self.items.len());
                self.items.push((key.to_vec(), 1));
            }
        }
    }

    /// `d.get(key, 0)`.
    pub fn get(&self, key: &[u32]) -> i64 {
        self.index.get(key).map_or(0, |&i| self.items[i].1)
    }

    /// `d.items()`, in insertion order.
    pub fn items(&self) -> &[(PyStr, i64)] {
        &self.items
    }
}

/// `_EvalWalk`: the counts and rows the walk of the event log gathers.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct EvalWalk<'a> {
    pub review_rows: Vec<&'a PyValue>,
    pub surface_rows: Vec<&'a PyValue>,
    pub retrieval_occurrences: i64,
    pub explicit_endorsements_with_excerpt: i64,
    pub applied_total: i64,
    pub applied_explicit: i64,
    pub applied_model: i64,
    pub applied_hook: i64,
    pub total_events_scanned: i64,
    pub events_in_window: i64,
    /// Search deliveries only: the cold-endorsement floor's basis.
    pub search_delivery_count: Counts,
    pub auto_applied_count: Counts,
    pub explicit_applied_count: Counts,
    /// Counted over the whole log, ahead of the window filter.
    pub hook_telemetry_events: i64,
    /// The in-window `turn_audited` and `search_miss` events, in order.
    pub audit_telemetry: Vec<&'a PyValue>,
    pub latest_miss_cutoff: Option<DateTime>,
    pub acknowledged_miss_event_ids: HashSet<PyStr>,
}

/// One `by_model` bucket: `{"audited": 0, "no_signal": 0, "misses": 0}`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ModelCounts {
    pub audited: i64,
    pub no_signal: i64,
    pub misses: i64,
}

impl ModelCounts {
    /// The bucket as the reference's dict, its keys in their order.
    pub fn pairs(&self) -> Vec<(PyStr, i64)> {
        vec![
            (cps("audited"), self.audited),
            (cps("no_signal"), self.no_signal),
            (cps("misses"), self.misses),
        ]
    }
}

/// `_AuditTally`: the audit telemetry counted once the markers are
/// resolved.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct AuditTally {
    pub turns_audited: i64,
    pub turns_no_signal: i64,
    pub silent_misses: i64,
    /// The last `silent_miss_limit` candidates, oldest first.
    pub silent_miss_buffer: Vec<SilentMissCandidate>,
    /// The last non-empty rule seen; empty when none was.
    pub threshold_rule: PyStr,
    pub repeat_audits: i64,
    /// In first-seen order.
    pub by_model: Vec<(PyStr, ModelCounts)>,
}

/// Whether the code points `s` spell the ASCII `text`.
fn spells(s: &[u32], text: &str) -> bool {
    s.len() == text.len() && s.iter().zip(text.bytes()).all(|(&c, b)| c == u32::from(b))
}

/// `value == text` for a `str` literal: only a `str` of those code points.
fn is_text(value: Option<&PyValue>, text: &str) -> bool {
    matches!(value, Some(PyValue::Str(s)) if spells(s, text))
}

/// `ev.get(key)`: the last entry of a `str` key, as `PyValue::get` reads
/// it, without building the key's code points for each event.
fn get<'a>(ev: &'a PyValue, key: &str) -> Option<&'a PyValue> {
    let PyValue::Dict(items) = ev else {
        return None;
    };
    items.iter().rev().find_map(|(k, v)| match k {
        PyValue::Str(s) if spells(s, key) => Some(v),
        _ => None,
    })
}

/// `isinstance(value, str) and value.strip()`.
fn has_text(value: &PyValue) -> bool {
    matches!(value, PyValue::Str(s) if !py_strip(s).is_empty())
}

/// `ev.get(a) or ev.get(b) or ...`: the first truthy value, None when
/// every one is falsy (the reference's `or []`, which counts nothing).
fn first_truthy<'a>(ev: &'a PyValue, keys: &[&str]) -> Option<&'a PyValue> {
    keys.iter().filter_map(|k| get(ev, k)).find(|v| truthy(v))
}

/// `_parse_ts(value)`: a `str` that parses, stamped UTC when naive.
fn parse_ts(value: Option<&PyValue>) -> Option<DateTime> {
    match value {
        Some(PyValue::Str(text)) => parse_event_ts(text),
        _ => None,
    }
}

/// `a < b` of two datetimes: two aware ones as instants, two naive ones by
/// their fields, a naive one against an aware one TypeError.
fn before(a: &DateTime, b: &DateTime) -> Answer<bool> {
    match (a.instant_us(), b.instant_us()) {
        (Some(x), Some(y)) => Ok(x < y),
        (None, None) => Ok(a.wall_micros() < b.wall_micros()),
        _ => Err(PyError::TypeError.into()),
    }
}

/// `_count_search`: one in-window `search` or `list` event's deliveries.
/// `returned`, then the legacy `memory_ids` and `hit_ids`, by truthiness;
/// a value that is not a list counts nothing. Each `str` id the scope
/// passes is a retrieval occurrence, and for a `search` also a floor
/// delivery.
pub fn count_search(
    walk: &mut EvalWalk<'_>,
    ev: &PyValue,
    kind: Option<&PyValue>,
    passes_scope: &mut dyn FnMut(Option<&PyStr>) -> bool,
) {
    let Some(PyValue::List(returned)) = first_truthy(ev, &["returned", "memory_ids", "hit_ids"])
    else {
        return;
    };
    let search = is_text(kind, "search");
    for mid in returned {
        let PyValue::Str(mid) = mid else { continue };
        if !passes_scope(Some(mid)) {
            continue;
        }
        walk.retrieval_occurrences += 1;
        if search {
            walk.search_delivery_count.bump(mid);
        }
    }
}

/// `_count_use`: one in-window `use` event's applies. The tier first (no
/// tier ends), then `is_settlement` (which may raise) before the ids are
/// looked at; the ids deduplicated in first-seen order, each keeping
/// whether its excerpt is a non-empty `str` after `strip`, upgraded by a
/// later one that is; then per unique id the scope test, the per-memory
/// count of its tier, and (a settlement stops here) the trio's counts.
pub fn count_use(
    walk: &mut EvalWalk<'_>,
    ev: &PyValue,
    passes_scope: &mut dyn FnMut(Option<&PyStr>) -> bool,
) -> Answer<()> {
    let Some(tier) = endorsement_tier(ev)? else {
        return Ok(());
    };
    let settlement = is_settlement(ev)?;
    let Some(PyValue::List(ids)) = first_truthy(ev, &["ids", "memory_ids"]) else {
        return Ok(());
    };
    let is_auto = tier == AppliedTier::Auto;
    // `list(raw)` when a list, else `[None] * len(ids)`: no excerpt.
    let excerpts: &[PyValue] = match get(ev, "claim_excerpts") {
        Some(PyValue::List(raw)) => raw,
        _ => &[],
    };
    let excerpt_has_text = |i: usize| excerpts.get(i).is_some_and(has_text);
    let mut seen: Vec<(&PyStr, bool)> = Vec::new();
    let mut at: HashMap<&[u32], usize> = HashMap::new();
    for (i, mid) in ids.iter().enumerate() {
        let PyValue::Str(mid) = mid else { continue };
        match at.get(mid.as_slice()) {
            Some(&j) => {
                if !seen[j].1 && excerpt_has_text(i) {
                    seen[j].1 = true;
                }
            }
            None => {
                at.insert(mid, seen.len());
                seen.push((mid, excerpt_has_text(i)));
            }
        }
    }
    for (mid, with_excerpt) in seen {
        if !passes_scope(Some(mid)) {
            continue;
        }
        if is_auto {
            walk.auto_applied_count.bump(mid);
        } else {
            walk.explicit_applied_count.bump(mid);
        }
        if settlement {
            continue;
        }
        walk.applied_total += 1;
        if !is_auto {
            walk.applied_explicit += 1;
            if tier == AppliedTier::Hook {
                walk.applied_hook += 1;
            } else {
                walk.applied_model += 1;
            }
            if with_excerpt {
                walk.explicit_endorsements_with_excerpt += 1;
            }
        }
    }
    Ok(())
}

/// Whether the event is in the window: only asked when a bound is set;
/// an unreadable `ts` is out; `ts >= cutoff`, then `ts < window_end`, each
/// by instant and the second only when the first holds.
fn in_window(
    ev: &PyValue,
    cutoff: Option<&DateTime>,
    window_end: Option<&DateTime>,
) -> Answer<bool> {
    if cutoff.is_none() && window_end.is_none() {
        return Ok(true);
    }
    let Some(ts) = parse_ts(get(ev, "ts")) else {
        return Ok(false);
    };
    if let Some(c) = cutoff {
        if before(&ts, c)? {
            return Ok(false);
        }
    }
    match window_end {
        Some(end) => before(&ts, end),
        None => Ok(true),
    }
}

/// `_walk_eval_events`: one pass over the log, per event in the
/// reference's order of effects: a value that is not a dict is skipped
/// uncounted; the scan count; the window test; the review and surface
/// rows (kept before the window filter); the hook-telemetry tally; the
/// two markers (`silent_miss_cutoff`, the latest readable `cutoff_ts`
/// winning when strictly later; `miss_ack`, a non-empty `str` `event_id`),
/// each ahead of the window skip; then the in-window rows by kind.
pub fn walk_eval_events<'a>(
    events: &'a [PyValue],
    cutoff: Option<&DateTime>,
    window_end: Option<&DateTime>,
    passes_scope: &mut dyn FnMut(Option<&PyStr>) -> bool,
) -> Answer<EvalWalk<'a>> {
    let mut walk = EvalWalk::default();
    for ev in events {
        if !matches!(ev, PyValue::Dict(_)) {
            continue;
        }
        walk.total_events_scanned += 1;
        let in_window = in_window(ev, cutoff, window_end)?;
        if in_window {
            walk.events_in_window += 1;
        }
        if is_review_row(ev)? {
            walk.review_rows.push(ev);
        }
        if is_surface_row(ev)? {
            walk.surface_rows.push(ev);
        }
        let kind = get(ev, "kind");
        if is_hook_telemetry_event(ev)? {
            walk.hook_telemetry_events += 1;
        }
        if is_text(kind, "silent_miss_cutoff") {
            if let Some(parsed) = parse_ts(get(ev, "cutoff_ts")) {
                let later = match &walk.latest_miss_cutoff {
                    None => true,
                    Some(latest) => before(latest, &parsed)?,
                };
                if later {
                    walk.latest_miss_cutoff = Some(parsed);
                }
            }
            continue;
        }
        if is_text(kind, "miss_ack") {
            if let Some(PyValue::Str(target)) = get(ev, "event_id") {
                if !target.is_empty() {
                    walk.acknowledged_miss_event_ids.insert(target.clone());
                }
            }
            continue;
        }
        if !in_window {
            continue;
        }
        if is_text(kind, "search") || is_text(kind, "list") {
            count_search(&mut walk, ev, kind, passes_scope);
        } else if is_text(kind, "show") {
            if let Some(PyValue::Str(mid)) = get(ev, "id") {
                if passes_scope(Some(mid)) {
                    walk.retrieval_occurrences += 1;
                }
            }
        } else if is_text(kind, "use") {
            count_use(&mut walk, ev, passes_scope)?;
        } else if is_text(kind, "turn_audited") || is_text(kind, "search_miss") {
            walk.audit_telemetry.push(ev);
        }
    }
    Ok(walk)
}

/// The bucket of `model` in `by_model`, made on first sight.
fn bucket<'t>(
    by_model: &'t mut Vec<(PyStr, ModelCounts)>,
    at: &mut HashMap<PyStr, usize>,
    model: &PyStr,
) -> &'t mut ModelCounts {
    let i = *at.entry(model.clone()).or_insert_with(|| {
        by_model.push((model.clone(), ModelCounts::default()));
        by_model.len() - 1
    });
    &mut by_model[i].1
}

/// A non-empty `str` value.
fn non_empty_text(value: Option<&PyValue>) -> Option<&PyStr> {
    match value {
        Some(PyValue::Str(s)) if !s.is_empty() => Some(s),
        _ => None,
    }
}

/// `_resolve_audit_telemetry`: the buffered events in order. Under a
/// cutoff, an event whose `ts` is unreadable or before it is dropped from
/// everything. A `turn_audited` with a truthy `repeat` counts only as a
/// repeat; else it is a no-signal turn or an audited one, bucketed under a
/// non-empty `client_model`. A `search_miss` is dropped by a tombstoned
/// top hit (asked only when there are tombstones) and then by an
/// acknowledged `event_id`; else it counts, buckets, and its candidate
/// enters the buffer, which a limit of 0 or below empties on every append
/// and any other limit trims to its last `limit`. Both kinds set the rule
/// from a non-empty `str` `threshold_rule`.
pub fn resolve_audit_telemetry(
    walk: &EvalWalk<'_>,
    tombstoned_ids: &HashSet<PyStr>,
    silent_miss_limit: i64,
) -> Answer<AuditTally> {
    let mut tally = AuditTally::default();
    let mut models: HashMap<PyStr, usize> = HashMap::new();
    let mut buffer: VecDeque<SilentMissCandidate> = VecDeque::new();
    for &ev in &walk.audit_telemetry {
        if let Some(cutoff) = &walk.latest_miss_cutoff {
            match parse_ts(get(ev, "ts")) {
                None => continue,
                Some(ts) if before(&ts, cutoff)? => continue,
                Some(_) => {}
            }
        }
        let model = non_empty_text(get(ev, "client_model"));
        if is_text(get(ev, "kind"), "turn_audited") {
            if get(ev, "repeat").is_some_and(truthy) {
                tally.repeat_audits += 1;
                continue;
            }
            let no_signal = is_text(get(ev, "verdict"), "no_signal");
            if no_signal {
                tally.turns_no_signal += 1;
            } else {
                tally.turns_audited += 1;
            }
            if let Some(model) = model {
                let b = bucket(&mut tally.by_model, &mut models, model);
                if no_signal {
                    b.no_signal += 1;
                } else {
                    b.audited += 1;
                }
            }
            if let Some(rule) = non_empty_text(get(ev, "threshold_rule")) {
                tally.threshold_rule = rule.clone();
            }
        } else {
            if !tombstoned_ids.is_empty() {
                if let Some(top) = canonical_top_hit_id(ev)? {
                    if tombstoned_ids.contains(&top) {
                        continue;
                    }
                }
            }
            if let Some(PyValue::Str(event_id)) = get(ev, "event_id") {
                if walk.acknowledged_miss_event_ids.contains(event_id) {
                    continue;
                }
            }
            tally.silent_misses += 1;
            if let Some(model) = model {
                bucket(&mut tally.by_model, &mut models, model).misses += 1;
            }
            if let Some(rule) = non_empty_text(get(ev, "threshold_rule")) {
                tally.threshold_rule = rule.clone();
            }
            if let Some(candidate) = silent_miss_from_event(ev)? {
                buffer.push_back(candidate);
                if silent_miss_limit <= 0 {
                    buffer.clear();
                } else if buffer.len() as u128 > silent_miss_limit as u128 {
                    buffer.pop_front();
                }
            }
        }
    }
    tally.silent_miss_buffer = buffer.into();
    Ok(tally)
}

/// What `_cold_endorsement_rows` returns: the rows, their total and
/// whether the bucket was suppressed.
#[derive(Clone, Debug, PartialEq)]
pub struct ColdRows {
    pub rows: Vec<ColdEndorsementMemoriesRow>,
    pub total: i64,
    pub suppressed: bool,
}

/// `_cold_endorsement_rows`: suppressed (no rows, a total of 0) when the
/// log holds no hook telemetry; else per floor delivery count in insertion
/// order: at the floor or above, weakly endorsed over its auto and
/// explicit applies, a memory of `by_id` that is not ambient; each row
/// with `first_summary_line` of the body; sorted by retrieval count
/// descending, then id in code point order, ties kept in order.
pub fn cold_endorsement_rows(
    walk: &EvalWalk<'_>,
    by_id: &HashMap<PyStr, Memory>,
    floor: i64,
    ratio_threshold: f64,
) -> ColdRows {
    let suppressed = walk.hook_telemetry_events == 0;
    let mut rows = Vec::new();
    if !suppressed {
        for (mid, rcount) in walk.search_delivery_count.items() {
            if *rcount < floor {
                continue;
            }
            let auto = walk.auto_applied_count.get(mid);
            let explicit = walk.explicit_applied_count.get(mid);
            if !weakly_endorsed(auto + explicit, explicit, ratio_threshold) {
                continue;
            }
            let Some(mem) = by_id.get(mid) else { continue };
            if mem.fields.category == Some(Category::Ambient) {
                continue;
            }
            rows.push(ColdEndorsementMemoriesRow {
                id: mem.fields.id.clone(),
                scopes: mem.fields.scopes.clone(),
                summary: first_summary_line(&mem.fields.body, 80),
                retrieval_count: *rcount,
                auto_applied_count: auto,
                explicit_applied_count: explicit,
            });
        }
        rows.sort_by(|a, b| {
            b.retrieval_count
                .cmp(&a.retrieval_count)
                .then_with(|| a.id.cmp(&b.id))
        });
    }
    ColdRows {
        total: rows.len() as i64,
        rows,
        suppressed,
    }
}
