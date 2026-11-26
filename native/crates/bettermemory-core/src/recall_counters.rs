//! `recall_counters.py`, the whole module: the recall surface's counters
//! read off the event log. `compute_surface_report` is the `surfaces`
//! section of `bettermemory eval`; the readers `is_surface_row`,
//! `is_surface_show`, `surface_of`, `shown_ids` and `session_of` are the
//! module's public names, and `_ids`, `_in_window` and `_attested` are
//! public here as [`ids_of`], [`in_window`] and [`attested`] so the
//! fixtures can pin them. `_conversation_rows`, `_reshown`, `_next_show`,
//! `_latest_grades` and `_SurfaceTally` are private.
//!
//! An event is a `PyValue` (a `dict` as the log reads it); a value that
//! is not a dict is skipped, never raised on. The window's bounds come
//! from eval's window, always aware, and every stamp `parse_event_ts`
//! answers is aware, so stamps compare as instants (microseconds). The
//! pick is the caller's closure; its error type is the caller's, so
//! `compute_surface_report` answers `Result<_, E>` with `E: From<PyError>`
//! for the `TypeError`s the reference raises. `review_unanswered`'s
//! `chains` sums ints of any size exactly, as decimal text.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fmt::Write as _;

use crate::datetime::{parse_event_ts, DateTime};
use crate::eval_model::RateCI;
use crate::events::{truthy, HARNESS_SESSION_FIELD, HARNESS_STAMPED_KINDS};
use crate::pyvalue::{cmp_int_text, PyError, PyStr, PyValue};
use crate::recall_state::{agent_of, is_settlement, SURFACE_BRIEF, SURFACE_RECALL};
use crate::sessions::{hook_spans, is_out_of_process, runs_beside};
use crate::unicode::{cps, py_strip};

/// `RECALL_GRADE`: the event kind `bettermemory recall grade` writes.
pub const RECALL_GRADE: &str = "recall_grade";
/// `STRATUM_DELIVERY`.
pub const STRATUM_DELIVERY: &str = "delivery";
/// `STRATUM_BRIEF`.
pub const STRATUM_BRIEF: &str = "brief";
/// `STRATUM_SILENT`.
pub const STRATUM_SILENT: &str = "silent";
/// `STRATA`, in the reference's order.
pub const STRATA: [&str; 3] = [STRATUM_DELIVERY, STRATUM_BRIEF, STRATUM_SILENT];
/// `SURFACES`, in the reference's order.
pub const SURFACES: [&str; 2] = [SURFACE_BRIEF, SURFACE_RECALL];
/// `_ROW_KINDS` beyond `HARNESS_STAMPED_KINDS`.
const OWN_KINDS: [&str; 6] = [
    "session_brief",
    "prompt_recall",
    "turn_audited",
    "segment_opened",
    "review_unanswered",
    RECALL_GRADE,
];
/// `SILENT_MISS_NOTE`: the sentence the section carries about the miss
/// rate beside it.
pub const SILENT_MISS_NOTE: &str =
    "silent_miss_rate reads differently once the recall surface runs: \
the old miss lane turned a would-be miss into a delivery, while the recall delivers project \
memories only, so a miss it does not serve is flagged again. Do not compare the rate across \
the switch.";

static NONE: PyValue = PyValue::None;

fn field<'a>(event: &'a PyValue, name: &str) -> &'a PyValue {
    event.get(name).unwrap_or(&NONE)
}

/// `value == text` for an ASCII `text`: only a `str` of those code points.
fn spells(value: &PyValue, text: &str) -> bool {
    matches!(value, PyValue::Str(s) if s.len() == text.len()
        && s.iter().zip(text.bytes()).all(|(&c, b)| c == u32::from(b)))
}

/// `a or b` over two fields.
fn either<'a>(event: &'a PyValue, first: &str, second: &str) -> &'a PyValue {
    let value = field(event, first);
    if truthy(value) {
        value
    } else {
        field(event, second)
    }
}

/// Whether Python can hash the value: no list or dict, nor a tuple
/// holding one at any depth.
fn hashable(value: &PyValue) -> bool {
    let mut stack = vec![value];
    while let Some(v) = stack.pop() {
        match v {
            PyValue::List(_) | PyValue::Dict(_) => return false,
            PyValue::Tuple(items) => stack.extend(items),
            _ => {}
        }
    }
    true
}

/// `_str`: a non-empty `str`, else None.
fn str_of(value: &PyValue) -> Option<&PyStr> {
    match value {
        PyValue::Str(s) if !s.is_empty() => Some(s),
        _ => None,
    }
}

/// The instant of an aware stamp, in microseconds.
fn at(dt: &DateTime) -> i128 {
    dt.instant_us().unwrap_or_else(|| dt.wall_micros())
}

/// `parse_event_ts(event.get("ts"))` as an instant: None for a non-`str`.
fn stamp_of(event: &PyValue) -> Option<i128> {
    match field(event, "ts") {
        PyValue::Str(text) => parse_event_ts(text).map(|dt| at(&dt)),
        _ => None,
    }
}

/// `is_surface_row`: `event.get("kind") in _ROW_KINDS`, `frozenset`
/// membership, so an unhashable kind raises `TypeError` and a hashable
/// non-`str` is simply absent.
pub fn is_surface_row(event: &PyValue) -> Result<bool, PyError> {
    let kind = field(event, "kind");
    if !hashable(kind) {
        return Err(PyError::TypeError);
    }
    Ok(OWN_KINDS
        .iter()
        .chain(HARNESS_STAMPED_KINDS)
        .any(|k| spells(kind, k)))
}

/// `is_surface_show`: a `session_brief` with ids, or a `prompt_recall`
/// whose `rule` is a `str` (the empty one included).
pub fn is_surface_show(event: &PyValue) -> bool {
    let kind = field(event, "kind");
    if spells(kind, "session_brief") {
        return !ids_of(field(event, "ids")).is_empty();
    }
    spells(kind, "prompt_recall") && matches!(field(event, "rule"), PyValue::Str(_))
}

/// `_anchors_conversation`: a brief (printed or not), a Stop's
/// `turn_audited`, or a show of either surface.
fn anchors_conversation(event: &PyValue) -> bool {
    let kind = field(event, "kind");
    spells(kind, "session_brief") || spells(kind, "turn_audited") || is_surface_show(event)
}

/// `surface_of`: the surface a show row was made by, else None.
pub fn surface_of(event: &PyValue) -> Option<&'static str> {
    if !is_surface_show(event) {
        return None;
    }
    Some(if spells(field(event, "kind"), "session_brief") {
        SURFACE_BRIEF
    } else {
        SURFACE_RECALL
    })
}

/// `shown_ids`: `ids` of a `session_brief`, `shown_ids` of any other row.
pub fn shown_ids(event: &PyValue) -> Vec<PyStr> {
    let name = if spells(field(event, "kind"), "session_brief") {
        "ids"
    } else {
        "shown_ids"
    };
    ids_of(field(event, name))
}

/// `session_of`: `session or session_id` (Python truthiness), kept only as
/// a non-empty `str`.
pub fn session_of(event: &PyValue) -> Option<&PyStr> {
    str_of(either(event, "session", "session_id"))
}

/// `_ids`: a non-empty `str` is one id; a list keeps its non-empty `str`
/// items, first seen first; anything else (a tuple, a dict) is none.
pub fn ids_of(value: &PyValue) -> Vec<PyStr> {
    match value {
        PyValue::Str(s) if !s.is_empty() => vec![s.clone()],
        PyValue::List(items) => {
            let mut seen = HashSet::new();
            let mut out = Vec::new();
            for item in items {
                if let PyValue::Str(s) = item {
                    if !s.is_empty() && seen.insert(s) {
                        out.push(s.clone());
                    }
                }
            }
            out
        }
        _ => Vec::new(),
    }
}

fn window_at(stamp: Option<i128>, start: Option<i128>, end: Option<i128>) -> bool {
    match stamp {
        None => start.is_none() && end.is_none(),
        Some(s) => start.is_none_or(|a| s >= a) && end.is_none_or(|b| s < b),
    }
}

/// `_in_window`: `start <= stamp < end` as instants, a missing bound
/// open; a missing stamp is inside only when both bounds are missing.
pub fn in_window(stamp: Option<DateTime>, start: Option<DateTime>, end: Option<DateTime>) -> bool {
    window_at(
        stamp.as_ref().map(at),
        start.as_ref().map(at),
        end.as_ref().map(at),
    )
}

/// `_attested`: an applied `use` row, not the auto fallback (`auto is
/// True`, the bool only, or `attribution == "auto"`), whose `ids or
/// memory_ids` and `claim_excerpts` are lists with a non-blank `str`
/// excerpt at an index whose id equals `memory_id`.
pub fn attested(event: &PyValue, memory_id: &[u32]) -> bool {
    if !spells(field(event, "outcome"), "applied")
        || matches!(field(event, "auto"), PyValue::Bool(true))
        || spells(field(event, "attribution"), "auto")
    {
        return false;
    }
    let (PyValue::List(ids), PyValue::List(excerpts)) = (
        either(event, "ids", "memory_ids"),
        field(event, "claim_excerpts"),
    ) else {
        return false;
    };
    ids.iter().enumerate().any(|(index, value)| {
        matches!(value, PyValue::Str(s) if s == memory_id)
            && matches!(excerpts.get(index), Some(PyValue::Str(e)) if !py_strip(e).is_empty())
    })
}

fn int(n: usize) -> PyValue {
    PyValue::Int(n.to_string())
}

fn dict(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::Str(cps(k)), v))
            .collect(),
    )
}

/// `_rate`: `RateCI.from_counts(numerator, denominator).to_dict()`.
fn rate(numerator: usize, denominator: usize) -> PyValue {
    let count = |n: usize| i64::try_from(n).unwrap_or(i64::MAX);
    RateCI::from_counts(count(numerator), count(denominator)).to_dict()
}

/// `_SurfaceTally`.
#[derive(Default)]
struct Tally {
    deliveries: usize,
    shown: usize,
    opened: usize,
    used: usize,
    ignored: usize,
    helped: usize,
    chars: Vec<String>,
    suppressed: BTreeMap<PyStr, usize>,
}

impl Tally {
    fn into_dict(mut self, with_suppressed: bool) -> PyValue {
        // statistics.median_low: the lower middle of the sorted ints.
        self.chars.sort_by(|a, b| cmp_int_text(a, b));
        let p50 = match self.chars.len() {
            0 => PyValue::None,
            n => PyValue::Int(self.chars[(n - 1) / 2].clone()),
        };
        let mut out = vec![
            ("deliveries", int(self.deliveries)),
            ("shown", int(self.shown)),
            ("opened", int(self.opened)),
            ("used", int(self.used)),
            ("ignored", int(self.ignored)),
            ("deliveries_helped", int(self.helped)),
            ("chars_p50", p50),
            ("shown_opened_rate", rate(self.opened, self.shown)),
            ("shown_used_rate", rate(self.used, self.shown)),
            ("ignored_rate", rate(self.ignored, self.shown)),
            ("delivery_helped_rate", rate(self.helped, self.deliveries)),
        ];
        if with_suppressed {
            let items = self.suppressed.iter();
            let items = items.map(|(k, &n)| (PyValue::Str(k.clone()), int(n)));
            out.push(("suppressed", PyValue::Dict(items.collect())));
        }
        dict(out)
    }
}

/// `for entry in suppressed_ids or []`, counting each dict entry's `str`
/// `reason`. A `str` yields its characters and a dict its keys, none of
/// them a dict; a truthy value that is not iterable raises `TypeError`.
fn count_suppressed(value: &PyValue, into: &mut BTreeMap<PyStr, usize>) -> Result<(), PyError> {
    let entries = match value {
        _ if !truthy(value) => return Ok(()),
        PyValue::List(items) | PyValue::Tuple(items) => items,
        PyValue::Str(_) | PyValue::Dict(_) => return Ok(()),
        _ => return Err(PyError::TypeError),
    };
    for entry in entries {
        if let (PyValue::Dict(_), PyValue::Str(reason)) = (entry, field(entry, "reason")) {
            *into.entry(reason.clone()).or_default() += 1;
        }
    }
    Ok(())
}

/// `_After`: a `show` or `use` row joined to a conversation.
struct After<'a> {
    at: i128,
    show: bool,
    ids: Vec<PyStr>,
    event: &'a PyValue,
}

/// `_conversation_rows`: the `show` and `use` rows with a readable stamp
/// before `end`, each under the conversation it joins, in log order.
fn conversation_rows<'a>(
    rows: &[&'a PyValue],
    stamps: &[Option<i128>],
    end: Option<i128>,
) -> Result<HashMap<&'a [u32], Vec<After<'a>>>, PyError> {
    let bounded: Vec<(&PyValue, i128)> = rows
        .iter()
        .zip(stamps)
        .filter_map(|(&row, &stamp)| stamp.map(|s| (row, s)))
        .filter(|&(_, s)| end.is_none_or(|e| s < e))
        .collect();
    let mut conversations: HashSet<&PyStr> = HashSet::new();
    let mut stamped: HashSet<&PyStr> = HashSet::new();
    // hook_spans reads only the hook rows (and raises on an unhashable
    // trigger), so the rows it would skip stay out of its copy.
    let mut hooks = Vec::new();
    for &(row, _) in &bounded {
        if anchors_conversation(row) {
            conversations.extend(session_of(row));
        }
        stamped.extend(str_of(field(row, HARNESS_SESSION_FIELD)));
        if !matches!(is_out_of_process(row), Ok(false)) {
            hooks.push(row.clone());
        }
    }
    let spans = hook_spans(&hooks)?;
    let mut latest: HashMap<&PyStr, &PyStr> = HashMap::new();
    let mut out: HashMap<&[u32], Vec<After>> = HashMap::new();
    for (row, stamp) in bounded {
        let worktree = str_of(field(row, "worktree_root"));
        let session = session_of(row);
        if anchors_conversation(row) {
            if let (Some(w), Some(s)) = (worktree, session) {
                latest.insert(w, s);
            }
            continue;
        }
        let kind = field(row, "kind");
        let show = spells(kind, "show");
        if !show && !spells(kind, "use") {
            continue;
        }
        let bridged = worktree
            .and_then(|w| latest.get(w).copied())
            .filter(|b| !stamped.contains(b));
        let conversation = match str_of(field(row, HARNESS_SESSION_FIELD)) {
            // A server keeps the id it was spawned with across a /clear or
            // /resume: a stamp whose hooks do not run beside the bridged
            // conversation is that window's own server under its old id.
            Some(own) => match bridged {
                Some(b) if own != b && !runs_beside(&spans, own, b) => b,
                _ => own,
            },
            None => match (session, bridged) {
                (Some(s), _) if conversations.contains(s) => s,
                (_, Some(b)) => b,
                _ => continue,
            },
        };
        let ids = if show {
            ids_of(field(row, "id"))
        } else {
            ids_of(either(row, "ids", "memory_ids"))
        };
        if !ids.is_empty() {
            let after = After {
                at: stamp,
                show,
                ids,
                event: row,
            };
            out.entry(conversation.as_slice()).or_default().push(after);
        }
    }
    Ok(out)
}

/// Per session and id, `(instant, position)` of each show before `end`.
type Reshown<'a> = HashMap<&'a [u32], HashMap<PyStr, Vec<(i128, usize)>>>;

/// `_reshown`: per conversation and id, when a show of either surface
/// put the id in front of the model before `end`, sorted.
fn reshown<'a>(rows: &[&'a PyValue], stamps: &[Option<i128>], end: Option<i128>) -> Reshown<'a> {
    let mut out: Reshown = HashMap::new();
    for (index, (&row, &stamp)) in rows.iter().zip(stamps).enumerate() {
        if !is_surface_show(row) {
            continue;
        }
        let (Some(session), Some(stamp)) = (session_of(row), stamp) else {
            continue;
        };
        if end.is_some_and(|e| stamp >= e) {
            continue;
        }
        let ids = out.entry(session.as_slice()).or_default();
        for memory_id in shown_ids(row) {
            ids.entry(memory_id).or_default().push((stamp, index));
        }
    }
    for times in out.values_mut().flat_map(HashMap::values_mut) {
        times.sort_unstable();
    }
    out
}

/// `_next_show`: `bisect_right` over the id's shows in the conversation,
/// so a show at the same instant and a later position ends this one's
/// follow-ups.
fn next_show(
    reshown: &Reshown,
    session: Option<&PyStr>,
    memory_id: &[u32],
    stamp: Option<i128>,
    index: usize,
) -> Option<i128> {
    let (session, stamp) = (session?, stamp?);
    let times = reshown.get(session.as_slice())?.get(memory_id)?;
    let position = times.partition_point(|&t| t <= (stamp, index));
    times.get(position).map(|t| t.0)
}

/// `_latest_grades` over rows already in the window (the reference reads
/// the window again, which keeps every row): per stratum, the graded ids
/// and how many were graded relevant, the latest grade of an id in a
/// sample counting.
fn latest_grades(rows: &[&PyValue]) -> [(usize, usize); 3] {
    let mut latest: HashMap<(&PyStr, PyStr), (usize, bool)> = HashMap::new();
    for &row in rows {
        let Some(sample) = str_of(field(row, "sample_id")) else {
            continue;
        };
        let stratum = field(row, "stratum");
        let Some(at) = STRATA.iter().position(|s| spells(stratum, s)) else {
            continue;
        };
        for (name, relevant) in [("relevant_ids", true), ("irrelevant_ids", false)] {
            for memory_id in ids_of(field(row, name)) {
                latest.insert((sample, memory_id), (at, relevant));
            }
        }
    }
    let mut out = [(0, 0); 3];
    for (at, relevant) in latest.into_values() {
        out[at].0 += 1;
        out[at].1 += usize::from(relevant);
    }
    out
}

/// Add the decimal digits to a little-endian base 10^9 magnitude.
fn add_digits(sum: &mut Vec<u64>, digits: &str) {
    const BASE: u64 = 1_000_000_000;
    for (pos, chunk) in digits.as_bytes().rchunks(9).enumerate() {
        let block = chunk.iter().fold(0, |n, &b| n * 10 + u64::from(b - b'0'));
        if pos == sum.len() {
            sum.push(0);
        }
        sum[pos] += block;
    }
    let mut carry = 0;
    for limb in sum.iter_mut() {
        *limb += carry;
        carry = *limb / BASE;
        *limb %= BASE;
    }
    if carry > 0 {
        sum.push(carry);
    }
}

/// The exact sum of canonical decimal ints, as canonical decimal text.
fn sum_ints<'a>(values: impl Iterator<Item = &'a str>) -> String {
    const BASE: u64 = 1_000_000_000;
    let (mut positive, mut negative) = (Vec::new(), Vec::new());
    for value in values {
        match value.strip_prefix('-') {
            Some(magnitude) => add_digits(&mut negative, magnitude),
            None => add_digits(&mut positive, value),
        }
    }
    for limbs in [&mut positive, &mut negative] {
        while limbs.last() == Some(&0) {
            limbs.pop();
        }
    }
    let order = positive
        .len()
        .cmp(&negative.len())
        .then_with(|| positive.iter().rev().cmp(negative.iter().rev()));
    let (mut larger, smaller, sign) = match order {
        std::cmp::Ordering::Equal => return "0".into(),
        std::cmp::Ordering::Greater => (positive, negative, ""),
        std::cmp::Ordering::Less => (negative, positive, "-"),
    };
    let mut borrow = 0;
    for (pos, limb) in larger.iter_mut().enumerate() {
        let take = smaller.get(pos).copied().unwrap_or(0) + borrow;
        borrow = u64::from(*limb < take);
        *limb = *limb + borrow * BASE - take;
    }
    while larger.last() == Some(&0) {
        larger.pop();
    }
    let mut out = String::from(sign);
    let mut limbs = larger.iter().rev();
    let _ = write!(out, "{}", limbs.next().copied().unwrap_or(0));
    for limb in limbs {
        let _ = write!(out, "{limb:09}");
    }
    out
}

/// A pick: the caller's choice of the shows tallied.
pub type Pick<'p, E> = &'p mut dyn FnMut(&PyValue) -> Result<bool, E>;

/// `compute_surface_report`: the surfaces section over `[start, end)`, or
/// None when the window holds no show, settlement, `segment_opened`,
/// `review_unanswered` or `recall_grade` row. `pick` chooses the shows
/// tallied, never whether the section is None; every row still joins and
/// every show still bounds the follow-ups of the id's earlier show.
pub fn compute_surface_report<E: From<PyError>>(
    events: &[PyValue],
    start: Option<DateTime>,
    end: Option<DateTime>,
    pick: Option<Pick<'_, E>>,
) -> Result<Option<PyValue>, E> {
    let mut rows = Vec::new();
    for event in events {
        if matches!(event, PyValue::Dict(_)) && is_surface_row(event)? {
            rows.push(event);
        }
    }
    let (start, end) = (start.as_ref().map(at), end.as_ref().map(at));
    let stamps: Vec<Option<i128>> = rows.iter().map(|row| stamp_of(row)).collect();
    let inside: Vec<bool> = stamps.iter().map(|&s| window_at(s, start, end)).collect();
    let shows: Vec<usize> = (0..rows.len())
        .filter(|&i| is_surface_show(rows[i]) && inside[i])
        .collect();
    let (mut settlements, mut opened_rows, mut unanswered, mut grades) =
        (vec![], vec![], vec![], vec![]);
    for (&row, _) in rows.iter().zip(&inside).filter(|(_, &inside)| inside) {
        if is_settlement(row)? {
            settlements.push(row);
        }
        let kind = field(row, "kind");
        if spells(kind, "segment_opened") {
            opened_rows.push(row);
        } else if spells(kind, "review_unanswered") {
            unanswered.push(row);
        } else if spells(kind, RECALL_GRADE) {
            grades.push(row);
        }
    }
    if shows.is_empty()
        && settlements.is_empty()
        && opened_rows.is_empty()
        && unanswered.is_empty()
        && grades.is_empty()
    {
        return Ok(None);
    }

    let after = conversation_rows(&rows, &stamps, end)?;
    let reshown = reshown(&rows, &stamps, end);
    let mut tallies = [Tally::default(), Tally::default()];
    let mut sessions: HashSet<&PyStr> = HashSet::new();
    // A segment is a cooldown's, the session's own or a subagent's.
    let mut segments: HashSet<(&PyStr, &[u32], &str)> = HashSet::new();
    let mut rules: BTreeSet<&PyStr> = BTreeSet::new();
    let picked = match pick {
        None => shows,
        Some(pick) => {
            let mut out = Vec::new();
            for index in shows {
                if pick(rows[index])? {
                    out.push(index);
                }
            }
            out
        }
    };
    for index in picked {
        let show = rows[index];
        let surface = usize::from(!spells(field(show, "kind"), "session_brief"));
        let tally = &mut tallies[surface];
        tally.deliveries += 1;
        if let PyValue::Int(chars) = field(show, "chars") {
            tally.chars.push(chars.clone());
        }
        rules.extend(str_of(field(show, "rule")));
        count_suppressed(field(show, "suppressed_ids"), &mut tally.suppressed)?;
        let session = session_of(show);
        if let Some(session) = session {
            sessions.insert(session);
            add_segment(&mut segments, session, show);
        }
        let stamp = stamps[index];
        let later: Vec<&After> = match (session.and_then(|s| after.get(s.as_slice())), stamp) {
            (Some(joined), Some(stamp)) => joined.iter().filter(|row| row.at >= stamp).collect(),
            _ => Vec::new(),
        };
        let mut helped = false;
        for memory_id in shown_ids(show) {
            tally.shown += 1;
            let until = next_show(&reshown, session, &memory_id, stamp, index);
            let (mut opened, mut used, mut ignored) = (false, false, false);
            for row in &later {
                if !row.ids.contains(&memory_id) || until.is_some_and(|u| row.at >= u) {
                    continue;
                }
                if row.show {
                    opened = true;
                    continue;
                }
                used = used || attested(row.event, &memory_id);
                ignored = ignored || spells(field(row.event, "outcome"), "ignored");
            }
            tally.opened += usize::from(opened);
            tally.used += usize::from(used);
            tally.ignored += usize::from(ignored);
            helped = helped || opened || used;
        }
        tally.helped += usize::from(helped);
    }
    for &row in &opened_rows {
        let Some(session) = session_of(row) else {
            continue;
        };
        sessions.insert(session);
        add_segment(&mut segments, session, row);
        rules.extend(str_of(field(row, "rule")));
    }

    let mut settled = [0usize; 2];
    for &row in &settlements {
        let surface = usize::from(spells(field(row, "surface"), SURFACE_RECALL));
        settled[surface] += ids_of(field(row, "ids")).len();
    }
    let chains = sum_ints(
        unanswered
            .iter()
            .filter_map(|row| match field(row, "count") {
                PyValue::Int(count) => Some(count.as_str()),
                _ => None,
            }),
    );
    let graded = latest_grades(&grades);
    let [brief, recall] = tallies;
    let rules = rules.into_iter().map(|r| PyValue::Str(r.clone())).collect();
    let grades = STRATA
        .iter()
        .zip(graded)
        .map(|(&stratum, (graded, relevant))| {
            let counts = vec![
                ("graded", int(graded)),
                ("relevant", int(relevant)),
                ("relevant_rate", rate(relevant, graded)),
            ];
            (PyValue::Str(cps(stratum)), dict(counts))
        });
    Ok(Some(dict(vec![
        ("rules", PyValue::List(rules)),
        (SURFACE_BRIEF, brief.into_dict(false)),
        (SURFACE_RECALL, recall.into_dict(true)),
        (
            "settlements",
            dict(vec![
                (SURFACE_BRIEF, int(settled[0])),
                (SURFACE_RECALL, int(settled[1])),
            ]),
        ),
        (
            "segments",
            dict(vec![
                ("sessions", int(sessions.len())),
                ("opened", int(segments.len())),
            ]),
        ),
        (
            "review_unanswered",
            dict(vec![
                ("turns", int(unanswered.len())),
                ("chains", PyValue::Int(chains)),
            ]),
        ),
        ("grades", PyValue::Dict(grades.collect())),
        ("note", PyValue::Str(cps(SILENT_MISS_NOTE))),
    ])))
}

/// `segments.add((session, agent_of(row) or "", segment))` for an int
/// `segment` that is neither a bool nor zero.
fn add_segment<'a>(
    segments: &mut HashSet<(&'a PyStr, &'a [u32], &'a str)>,
    session: &'a PyStr,
    row: &'a PyValue,
) {
    if let PyValue::Int(segment) = field(row, "segment") {
        if segment != "0" {
            let agent = agent_of(row).map_or(&[][..], Vec::as_slice);
            segments.insert((session, agent, segment.as_str()));
        }
    }
}
