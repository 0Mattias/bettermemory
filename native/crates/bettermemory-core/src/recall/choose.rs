//! `recall.py`'s hit choice and delivery: the two seen-readers
//! (`_seen_in_log`, `_seen_in_segment`), `_choose_hits` and
//! `_deliver_recall`, with `recall_admission._unless_standing` and
//! `_contradiction_open` and `recall_result.HookResult`.
//!
//! The turn (`recall_prompt._PromptTurn`) is the trait
//! [`PromptTurnView`]: its store's reads, its admission, its key, clock
//! and caller. The membership model's `admit_recall`, the recall state's
//! `claim_delivery`, `sessions.joined_sessions`, `generate_ulid`,
//! `recall_prompt._record_delivery` and the hold are arguments. Every
//! datetime handed in is aware, as the reference's callers make them.

use std::collections::{BTreeSet, HashMap, HashSet};

use super::render::{disputed_marks, render_recall_block, summarize, RecallPointer};
use super::{RECALL_MAX_POINTERS, SEEN_KINDS};
use crate::audit::report::{MissHit, MissReport};
use crate::control_tokens::defang_control_tokens;
use crate::datetime::{parse_event_ts, DateTime};
use crate::events::{str_ids, truthy, HARNESS_SESSION_FIELD};
use crate::in_force::{in_force, InForce, LinkReader, RecordView};
use crate::pyvalue::{PyStr, PyValue};
use crate::recall_settle::{Key, Sessions, RULE};
use crate::store::provenance::TrustRow;
use crate::unicode::cps;

/// `recall_state._DELIVERED_PROVENANCE`: `{LOCAL, IMPORTED}`.
pub const DELIVERED_PROVENANCE: [&str; 2] = ["local", "imported"];
/// `recall_state.SUMMARY_CHARS`, `summarize`'s default limit.
pub const SUMMARY_CHARS: i64 = 80;
/// The surface a delivery names.
pub const SURFACE: &str = "prompt_recall";

/// What the reference raises out of this unit.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ChooseError {
    /// An unhashable value tested against a set (`kind`, a session).
    TypeError,
    /// What `admit_recall` raised, by its class name.
    Raised(String),
    /// An admitted hit id holds a code point that is not a Unicode scalar
    /// (a lone surrogate). `in_force` reads ids as Rust strings, so such
    /// an id cannot reach it unchanged; the port declines rather than
    /// answer for a different id.
    NotUnicode,
}

/// The fields of `models.Memory` this unit reads.
#[derive(Clone, Debug, PartialEq)]
pub struct ChoiceMemory {
    pub id: PyStr,
    pub body: PyStr,
    pub updated: Option<DateTime>,
    pub last_verified_at: Option<DateTime>,
}

/// The store's reads this unit makes, past `in_force`'s.
pub trait ChooseStore: LinkReader {
    /// `store.events_of_kinds(kinds, since=since)`; the kinds are a set.
    fn events_of_kinds(&mut self, kinds: &[&str], since: &DateTime) -> Vec<PyValue>;
    fn load_many(&mut self, ids: &[PyStr]) -> Vec<ChoiceMemory>;
    /// `store.trust_rows(ids)`; None where the reference's is None.
    fn trust_rows(&mut self, ids: &[PyStr]) -> Option<HashMap<PyStr, TrustRow>>;
    /// `store.contradicted_at(ids)`, in the dict's order.
    fn contradicted_at(&mut self, ids: &[PyStr]) -> Vec<(PyStr, DateTime)>;
    /// `standing.standing_ids(store, admission, ids)`.
    fn standing_ids<V: RecordView>(&mut self, admission: &mut V, ids: &[PyStr]) -> Vec<PyStr>;
    /// `store.pending_conflict_pairs()`.
    fn pending_conflict_pairs(&mut self) -> Vec<(PyStr, PyStr, PyStr)>;
}

/// `recall_prompt._PromptTurn` as this unit reads it.
pub trait PromptTurnView {
    type Store: ChooseStore;
    type View: RecordView;
    /// `(turn.store, turn.admission)`.
    fn parts(&mut self) -> (&mut Self::Store, &mut Self::View);
    /// `turn.surface`, the admission's name.
    fn surface(&self) -> &[u32];
    fn key(&self) -> &Key;
    fn now(&self) -> &DateTime;
    /// `turn.caller.worktree_root`.
    fn worktree_root(&self) -> Option<&[u32]>;
    /// `turn.remint`.
    fn remint(&self) -> Option<&PyValue>;
}

/// `RecallState.claim_delivery(key, ids, now=, event_id=)`.
pub trait ClaimState {
    fn claim_delivery(
        &mut self,
        key: &Key,
        ids: &[PyStr],
        now: &DateTime,
        event_id: &[u32],
    ) -> (Vec<PyStr>, i64);
}

/// `recall_result.HookResult` (`recall_result.py`), as built.
#[derive(Clone, Debug, PartialEq)]
pub struct HookResult {
    pub stdout: PyStr,
    pub surface: PyStr,
    pub ids: Vec<PyStr>,
    pub chars: usize,
    pub rule: PyStr,
    pub event_id: Option<PyStr>,
    pub membership: Option<PyValue>,
}

impl HookResult {
    /// The constructor with `__post_init__`: `stdout` with each control
    /// token named, `chars` following it when it was the text's length.
    pub fn new(
        stdout: PyStr,
        surface: &str,
        ids: Vec<PyStr>,
        chars: usize,
        event_id: Option<PyStr>,
        membership: Option<PyValue>,
    ) -> Self {
        let named = defang_control_tokens(&stdout);
        let chars = if named != stdout && chars == stdout.len() {
            named.len()
        } else {
            chars
        };
        HookResult {
            stdout: named,
            surface: cps(surface),
            ids,
            chars,
            rule: cps(RULE),
            event_id,
            membership,
        }
    }
}

/// `_Chosen`: the hit, the record delivered in its slot, its trust row
/// and its `InForce`.
pub type Chosen = (MissHit, ChoiceMemory, TrustRow, Option<InForce>);

/// A held entry: `{"id", "reason"}` and `"relation"` when there is one.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Held {
    pub id: PyStr,
    pub reason: PyStr,
    pub relation: Option<PyStr>,
}

/// `_choose_hits`' answer.
#[derive(Clone, Debug, PartialEq)]
pub struct Choice {
    pub chosen: Vec<Chosen>,
    pub suppressed: Vec<Held>,
    pub rc3_candidates: Vec<ChoiceMemory>,
}

/// What `_record_delivery` is handed.
pub struct Delivery<'a> {
    pub block: &'a [u32],
    pub shown_ids: &'a [PyStr],
    pub event_id: &'a [u32],
    pub segment: i64,
    pub choice: &'a Choice,
}

static NONE: PyValue = PyValue::None;

fn field<'a>(event: &'a PyValue, name: &str) -> &'a PyValue {
    event.get(name).unwrap_or(&NONE)
}

fn either<'a>(first: &'a PyValue, second: &'a PyValue) -> &'a PyValue {
    if truthy(first) {
        first
    } else {
        second
    }
}

fn at(dt: &DateTime) -> i128 {
    dt.instant_us().unwrap_or_else(|| dt.wall_micros())
}

/// Whether `hash(value)` answers: no list or dict anywhere in a tuple.
/// Iterative, so a deeply nested tuple cannot exhaust the stack.
fn hashable(value: &PyValue) -> bool {
    let mut stack = vec![value];
    while let Some(v) = stack.pop() {
        match v {
            PyValue::List(_) | PyValue::Dict(_) => return false,
            PyValue::Tuple(items) => stack.extend(items.iter()),
            _ => {}
        }
    }
    true
}

/// `value in sessions` for a set of `str`.
fn member(value: &PyValue, sessions: &BTreeSet<PyStr>) -> Result<bool, ChooseError> {
    if !hashable(value) {
        return Err(ChooseError::TypeError);
    }
    Ok(matches!(value, PyValue::Str(s) if sessions.contains(s)))
}

/// `_seen_in_log`: the ids retrieved, written or updated in the segment.
pub fn seen_in_log(
    events: &[PyValue],
    since: &DateTime,
    own_since: &DateTime,
    sessions: &BTreeSet<PyStr>,
    worktree_root: Option<&[u32]>,
    harness_session: Option<&[u32]>,
) -> Result<BTreeSet<PyStr>, ChooseError> {
    let mut seen = BTreeSet::new();
    for event in events {
        let kind = field(event, "kind");
        if !hashable(kind) {
            return Err(ChooseError::TypeError);
        }
        let PyValue::Str(kind) = kind else { continue };
        if !SEEN_KINDS.iter().any(|k| cps(k) == *kind) {
            continue;
        }
        let stamp = match field(event, "ts") {
            PyValue::Str(text) => parse_event_ts(text),
            _ => None,
        };
        let Some(stamp) = stamp else { continue };
        let session = either(field(event, "session"), field(event, "session_id"));
        match field(event, HARNESS_SESSION_FIELD) {
            PyValue::Str(stamped) if !stamped.is_empty() => {
                let own = harness_session == Some(stamped.as_slice()) || member(session, sessions)?;
                if !own || at(&stamp) < at(own_since) {
                    continue;
                }
            }
            _ => {
                let own = member(session, sessions)? && at(&stamp) >= at(own_since);
                let here = worktree_root.is_some_and(|root| {
                    matches!(field(event, "worktree_root"), PyValue::Str(w) if w == root)
                        && at(&stamp) >= at(since)
                });
                if !(own || here) {
                    continue;
                }
            }
        }
        let word: String = kind.iter().filter_map(|&c| char::from_u32(c)).collect();
        if matches!(word.as_str(), "show" | "write" | "update") {
            let committed = match event.get("status") {
                None => true,
                Some(v) => matches!(v, PyValue::Str(s) if *s == cps("committed")),
            };
            if word != "show" && !committed {
                continue;
            }
            if let PyValue::Str(id) = field(event, "id") {
                seen.insert(id.clone());
            }
            continue;
        }
        let ids = either(
            either(field(event, "returned"), field(event, "memory_ids")),
            field(event, "hit_ids"),
        );
        seen.extend(str_ids(ids));
    }
    Ok(seen)
}

/// `_seen_in_segment`: the segment's shown ids and what the log saw.
/// `shown` and `started_at` are the `SegmentView`'s.
pub fn seen_in_segment<T: PromptTurnView, J: Sessions>(
    turn: &mut T,
    joined: &mut J,
    shown: &[PyStr],
    started_at: &DateTime,
    own_since: &DateTime,
    recent: &[PyValue],
) -> Result<BTreeSet<PyStr>, ChooseError> {
    let key = turn.key().clone();
    let root = turn.worktree_root().map(<[u32]>::to_vec);
    let mut sessions = joined.joined_sessions(recent, &key.1, root.as_deref());
    sessions.insert(key.1.clone());
    let since = if at(started_at) < at(own_since) {
        started_at
    } else {
        own_since
    };
    let events = turn.parts().0.events_of_kinds(&SEEN_KINDS, since);
    let mut seen = seen_in_log(
        &events,
        started_at,
        own_since,
        &sessions,
        root.as_deref(),
        Some(&key.1),
    )?;
    seen.extend(shown.iter().cloned());
    Ok(seen)
}

/// `_contradiction_open`.
pub fn contradiction_open(memory: &ChoiceMemory, contradicted: &HashMap<PyStr, DateTime>) -> bool {
    let Some(stamp) = contradicted.get(&memory.id) else {
        return false;
    };
    let mut answered = memory.updated.map(DateTime::assume_utc);
    let verified = memory.last_verified_at.map(DateTime::assume_utc);
    if let Some(v) = verified {
        if answered.as_ref().is_none_or(|a| at(&v) > at(a)) {
            answered = Some(v);
        }
    }
    answered.is_none_or(|a| at(stamp) > at(&a))
}

/// `_unless_standing`: `contradicted` less the standing records'.
pub fn unless_standing<S: ChooseStore, V: RecordView>(
    store: &mut S,
    admission: &mut V,
    contradicted: Vec<(PyStr, DateTime)>,
) -> Vec<(PyStr, DateTime)> {
    if contradicted.is_empty() {
        return contradicted;
    }
    let ids: Vec<PyStr> = contradicted.iter().map(|(id, _)| id.clone()).collect();
    let standing: HashSet<PyStr> = store.standing_ids(admission, &ids).into_iter().collect();
    contradicted
        .into_iter()
        .filter(|(id, _)| !standing.contains(id))
        .collect()
}

/// The id as a Rust string, or None when a code point is not a scalar.
fn text(id: &[u32]) -> Option<String> {
    id.iter().map(|&c| char::from_u32(c)).collect()
}

fn gate(
    memory: &ChoiceMemory,
    row: Option<&TrustRow>,
    contradicted: &HashMap<PyStr, DateTime>,
    seen: &BTreeSet<PyStr>,
) -> Option<&'static str> {
    match row {
        Some(r) if DELIVERED_PROVENANCE.contains(&r.label.as_str()) => {}
        _ => return Some("provenance"),
    }
    if contradiction_open(memory, contradicted) {
        return Some("contradicted");
    }
    if seen.contains(&memory.id) {
        return Some("seen_in_segment");
    }
    None
}

/// The segment's view as `_choose_hits` reads it.
pub struct Window<'a> {
    pub shown: &'a [PyStr],
    pub started_at: &'a DateTime,
    pub own_since: &'a DateTime,
    pub recent: &'a [PyValue],
}

/// `_choose_hits`. `admit(hits, surface)` is `model.admit_recall(
/// resolution, hits, admission=surface)`: `(delivered, suppressed)`.
pub fn choose_hits<T: PromptTurnView, J: Sessions>(
    turn: &mut T,
    joined: &mut J,
    report: &MissReport,
    window: &Window<'_>,
    admit: impl FnOnce(&[PyStr], &[u32]) -> Result<(Vec<PyStr>, Vec<(PyStr, PyStr)>), ChooseError>,
) -> Result<Choice, ChooseError> {
    let hit_ids: Vec<PyStr> = report.top_hits.iter().map(|h| h.id.clone()).collect();
    let surface = turn.surface().to_vec();
    let (delivered, suppressed_by) = admit(&hit_ids, &surface)?;
    let admitted_here: HashSet<PyStr> = delivered.into_iter().collect();
    let withheld: HashMap<PyStr, PyStr> = suppressed_by.into_iter().collect();
    let seen = seen_in_segment(
        turn,
        joined,
        window.shown,
        window.started_at,
        window.own_since,
        window.recent,
    )?;
    let asked: Vec<String> = hit_ids
        .iter()
        .filter(|id| admitted_here.contains(*id))
        .map(|id| text(id))
        .collect::<Option<_>>()
        .ok_or(ChooseError::NotUnicode)?;
    let (store, admission) = turn.parts();
    let forced_list = in_force(store, &asked, admission);
    let forced: HashMap<PyStr, InForce> = forced_list
        .iter()
        .map(|e| (cps(&e.asked), e.clone()))
        .collect();
    let own: HashSet<PyStr> = forced_list
        .iter()
        .filter(|e| !e.history() && !e.held)
        .map(|e| cps(&e.id))
        .collect();
    let mut wanted: Vec<PyStr> = Vec::new();
    let mut listed: HashSet<PyStr> = HashSet::new();
    for id in hit_ids
        .iter()
        .cloned()
        .chain(forced_list.iter().map(|e| cps(&e.id)))
    {
        if listed.insert(id.clone()) {
            wanted.push(id);
        }
    }
    let memories: HashMap<PyStr, ChoiceMemory> = store
        .load_many(&wanted)
        .into_iter()
        .map(|m| (m.id.clone(), m))
        .collect();
    let trust = store.trust_rows(&wanted);
    let marked = store.contradicted_at(&wanted);
    let contradicted: HashMap<PyStr, DateTime> = unless_standing(store, admission, marked)
        .into_iter()
        .collect();
    let trust_of = |id: &PyStr| trust.as_ref().and_then(|t| t.get(id));

    let mut choice = Choice {
        chosen: Vec::new(),
        suppressed: Vec::new(),
        rc3_candidates: Vec::new(),
    };
    for hit in &report.top_hits {
        let Some(mut memory) = memories.get(&hit.id) else {
            continue;
        };
        let mut row = trust_of(&hit.id);
        let mut held = gate(memory, row, &contradicted, &seen);
        if held.is_none() && choice.rc3_candidates.len() < RECALL_MAX_POINTERS {
            choice.rc3_candidates.push(memory.clone());
        }
        let forced_entry = forced.get(&hit.id);
        match forced_entry {
            Some(entry) if entry.held => held = Some("held"),
            Some(entry) if entry.history() => {
                let head_id = cps(&entry.id);
                row = trust_of(&head_id);
                match memories.get(&head_id) {
                    Some(head)
                        if !own.contains(&head_id)
                            && !seen.contains(&head_id)
                            && !choice.chosen.iter().any(|c| c.1.id == head_id) =>
                    {
                        memory = head;
                        held = gate(memory, row, &contradicted, &seen);
                    }
                    _ => held = Some("superseded"),
                }
            }
            _ => {}
        }
        let (reason, relation) = if !admitted_here.contains(&hit.id) {
            (Some("other_project"), withheld.get(&hit.id).cloned())
        } else if held.is_some() {
            (held, None)
        } else if choice.chosen.len() >= RECALL_MAX_POINTERS {
            continue;
        } else {
            (None, None)
        };
        if let Some(reason) = reason {
            choice.suppressed.push(Held {
                id: hit.id.clone(),
                reason: cps(reason),
                relation,
            });
            continue;
        }
        let row = row.expect("a delivered hit has its trust row").clone();
        choice
            .chosen
            .push((hit.clone(), memory.clone(), row, forced_entry.cloned()));
    }
    Ok(choice)
}

fn capped(pointers: &[&RecallPointer]) -> Vec<Held> {
    pointers
        .iter()
        .map(|p| Held {
            id: p.id.clone(),
            reason: cps("cap"),
            relation: None,
        })
        .collect()
}

/// `_deliver_recall`: the block claimed and recorded, or a hold.
/// `record` is `_record_delivery` and answers `str(fields["event_id"])`;
/// `hold(reason, entries)` is the caller's hold; `mint` is `generate_ulid`.
pub fn deliver_recall<T: PromptTurnView, C: ClaimState>(
    turn: &mut T,
    state: &mut C,
    choice: &Choice,
    mint: impl FnOnce() -> PyStr,
    record: impl FnOnce(&Delivery<'_>) -> PyStr,
    hold: impl FnOnce(&str, Vec<Held>) -> HookResult,
) -> HookResult {
    let pairs = turn.parts().0.pending_conflict_pairs();
    let marks: HashMap<PyStr, PyStr> = disputed_marks(&pairs).into_iter().collect();
    let pointers: Vec<RecallPointer> = choice
        .chosen
        .iter()
        .map(|(hit, memory, row, _)| RecallPointer {
            id: memory.id.clone(),
            summary: summarize(&memory.body, SUMMARY_CHARS),
            snippet: if memory.id == hit.id {
                hit.snippet.clone()
            } else {
                Vec::new()
            },
            provenance: Some(cps(&row.label)),
            remote_stamp: false,
            trust_unavailable: false,
            disputed: marks.get(&memory.id).cloned(),
        })
        .collect();
    let (mut block, mut shown_ids) = render_recall_block(&pointers);
    if shown_ids.is_empty() {
        return hold(
            "nothing_rendered",
            capped(&pointers.iter().collect::<Vec<_>>()),
        );
    }
    let event_id = mint();
    let key = turn.key().clone();
    let now = *turn.now();
    let (claimed, segment) = state.claim_delivery(&key, &shown_ids, &now, &event_id);
    if claimed.is_empty() {
        let entries = shown_ids
            .iter()
            .map(|id| Held {
                id: id.clone(),
                reason: cps("seen_in_segment"),
                relation: None,
            })
            .collect();
        return hold("segment_claimed", entries);
    }
    if claimed != shown_ids {
        let kept: HashSet<&PyStr> = claimed.iter().collect();
        let left: Vec<RecallPointer> = pointers
            .iter()
            .filter(|p| kept.contains(&p.id))
            .cloned()
            .collect();
        (block, shown_ids) = render_recall_block(&left);
        if shown_ids.is_empty() {
            return hold("nothing_rendered", capped(&left.iter().collect::<Vec<_>>()));
        }
    }
    let recorded = record(&Delivery {
        block: &block,
        shown_ids: &shown_ids,
        event_id: &event_id,
        segment,
        choice,
    });
    let chars = block.len();
    HookResult::new(
        block,
        SURFACE,
        shown_ids,
        chars,
        Some(recorded),
        turn.remint().cloned(),
    )
}
