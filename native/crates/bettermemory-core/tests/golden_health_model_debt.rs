//! Every row of `health_model_debt.jsonl` against unit health_model-2:
//! `clamp_ratio_threshold`, `weakly_endorsed` with `_is_weakly_endorsed`,
//! `unread_fields`, `_unread_dict` and each report type's `to_dict`; and
//! the two sweeps by count and digest, their inputs drawn here by the
//! generator's rule (unit 1's `_Draw`, `draw_memory` and `draw_float`).

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::float_repr;
use bettermemory_core::health_model::{self as hm, HealthModelError};
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::origin_history::HistoryUnread;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::Category;
use bettermemory_core::unicode;
use common::{assert_count, jsonl, neutral, text_field};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const NAME: &str = "health_model_debt.jsonl";

/// `_common.neutral` of a value `to_dict` answers.
fn to_neutral(v: &PyValue) -> Value {
    match v {
        PyValue::None => json!({"t": "null"}),
        PyValue::Bool(b) => json!({"t": "bool", "v": b}),
        PyValue::Int(t) => json!({"t": "int", "v": t}),
        PyValue::Float(f) => float_neutral(*f),
        PyValue::Str(s) => match unicode::to_string(s) {
            Some(text) => json!({"t": "str", "v": text}),
            None => json!({"t": "str", "v_cps": s}),
        },
        PyValue::List(items) => {
            json!({"t": "list", "v": items.iter().map(to_neutral).collect::<Vec<_>>()})
        }
        PyValue::Dict(items) => json!({"t": "dict", "v": items
            .iter()
            .map(|(k, v)| json!([to_neutral(k), to_neutral(v)]))
            .collect::<Vec<_>>()}),
        other => panic!("no answer holds {other:?}"),
    }
}

fn float_neutral(f: f64) -> Value {
    json!({"t": "float", "bits": format!("{:016x}", f.to_bits()), "repr": float_repr::repr(f)})
}

fn float_in(v: &Value) -> f64 {
    match neutral(v) {
        PyValue::Float(f) => f,
        other => panic!("not a float: {other:?}"),
    }
}

fn dt(v: &Value) -> DateTime {
    let f = v.as_array().expect("datetime fields");
    let u = |i: usize| f[i].as_u64().expect("a field") as u32;
    DateTime::new(
        f[0].as_i64().expect("year"),
        u(1),
        u(2),
        u(3),
        u(4),
        u(5),
        u(6),
        f[7].as_i64(),
    )
    .expect("a datetime")
}

fn opt_dt(v: &Value) -> Option<DateTime> {
    (!v.is_null()).then(|| dt(v))
}

fn i64_of(v: &Value) -> i64 {
    v.as_i64().expect("an i64")
}

fn int_or_zero(i: &Value, name: &str) -> i64 {
    i.get(name).map_or(0, i64_of)
}

fn err(e: HealthModelError) -> Value {
    json!({"x": e.name()})
}

fn answer(a: Result<PyValue, HealthModelError>) -> Value {
    a.map_or_else(err, |v| to_neutral(&v))
}

/// An optional text field: null is None, else `text_field`.
fn opt_text(i: &Value, name: &str) -> Option<PyStr> {
    (!i.get(name).is_some_and(Value::is_null)).then(|| text_field(i, name))
}

fn texts_of(v: Option<&Value>) -> Vec<PyStr> {
    v.map_or_else(Vec::new, |v| {
        v.as_array()
            .expect("texts")
            .iter()
            .map(|s| text_field(s, "v"))
            .collect()
    })
}

fn pairs(v: Option<&Value>) -> Vec<(PyStr, i64)> {
    v.map_or_else(Vec::new, |v| {
        v.as_array()
            .expect("pairs")
            .iter()
            .map(|p| (text_field(&p[0], "v"), i64_of(&p[1])))
            .collect()
    })
}

fn category(v: &Value) -> Option<Category> {
    v.as_str().map(|s| {
        Category::MEMBERS
            .iter()
            .find(|(t, _)| *t == s)
            .expect("a category")
            .1
    })
}

const COUNTS: [&str; 10] = [
    "retrieval_count",
    "show_count",
    "applied_count",
    "auto_applied_count",
    "explicit_applied_count",
    "hook_applied_count",
    "model_applied_count",
    "ignored_count",
    "contradicted_count",
    "corrected_count",
];

fn count_slot<'a>(m: &'a mut hm::MemoryStats, name: &str) -> &'a mut i64 {
    match name {
        "retrieval_count" => &mut m.retrieval_count,
        "show_count" => &mut m.show_count,
        "applied_count" => &mut m.applied_count,
        "auto_applied_count" => &mut m.auto_applied_count,
        "explicit_applied_count" => &mut m.explicit_applied_count,
        "hook_applied_count" => &mut m.hook_applied_count,
        "model_applied_count" => &mut m.model_applied_count,
        "ignored_count" => &mut m.ignored_count,
        "contradicted_count" => &mut m.contradicted_count,
        "corrected_count" => &mut m.corrected_count,
        other => panic!("no count {other}"),
    }
}

/// A `MemoryStats` from unit 1's spelling (`_memory_in`).
fn memory_of(i: &Value) -> hm::MemoryStats {
    let mut m = hm::MemoryStats::new(
        text_field(i, "id"),
        texts_of(i.get("scopes")),
        text_field(i, "summary"),
        dt(&i["created"]),
        dt(&i["updated"]),
    );
    for name in COUNTS {
        if let Some(v) = i.get(name) {
            *count_slot(&mut m, name) = i64_of(v);
        }
    }
    let opt = |name: &str| i.get(name).and_then(opt_dt);
    m.last_used_at = opt("last_used_at");
    m.last_contradicted_at = opt("last_contradicted_at");
    m.last_verified_at = opt("last_verified_at");
    m.last_corroborated = opt("last_corroborated");
    if let Some(c) = i.get("category") {
        m.category = category(c);
    }
    if let Some(t) = i.get("resolution_timeline") {
        let PyValue::List(ref items) = neutral(t) else {
            panic!("a timeline list")
        };
        m.resolution_timeline = items.clone();
    }
    m
}

fn memories(v: Option<&Value>) -> Vec<hm::MemoryStats> {
    v.map_or_else(Vec::new, |v| {
        v.as_array().expect("rows").iter().map(memory_of).collect()
    })
}

fn actor_of(i: &Value) -> hm::ActorSlice {
    hm::ActorSlice {
        client: opt_text(i, "client"),
        memories: int_or_zero(i, "memories"),
        memories_in_window: int_or_zero(i, "memories_in_window"),
        events: int_or_zero(i, "events"),
        searches: int_or_zero(i, "searches"),
        applies: int_or_zero(i, "applies"),
        models: pairs(i.get("models")),
        principals: pairs(i.get("principals")),
    }
}

fn drift_row_of(i: &Value) -> hm::CommitDriftRow {
    let mut r = hm::CommitDriftRow::new(
        text_field(i, "id"),
        texts_of(i.get("scopes")),
        text_field(i, "summary"),
        opt_dt(&i["last_verified_at"]),
        i64_of(&i["commits_since_verify"]),
    );
    if i.get("basis").is_some() || i.get("basis_cps").is_some() {
        r.basis = text_field(i, "basis");
    }
    r
}

fn drift_rows(v: Option<&Value>) -> Vec<hm::CommitDriftRow> {
    v.map_or_else(Vec::new, |v| {
        v.as_array()
            .expect("rows")
            .iter()
            .map(drift_row_of)
            .collect()
    })
}

fn group_of(i: &Value) -> hm::CrossRepoDriftGroup {
    let mut g = hm::CrossRepoDriftGroup::new(text_field(i, "repo"), text_field(i, "worktree_root"));
    g.rows = drift_rows(i.get("rows"));
    g.total_drifted = int_or_zero(i, "total_drifted");
    g.candidates = int_or_zero(i, "candidates");
    g.unread = int_or_zero(i, "unread");
    g.unread_causes = pairs(i.get("unread_causes"));
    g.unread_ids = texts_of(i.get("unread_ids"));
    g.last_checked_at = i.get("last_checked_at").and_then(opt_dt);
    g
}

fn cause(text: &str) -> HistoryUnread {
    HistoryUnread::from_value(text).expect("a cause")
}

fn check_clamp(row: &Value) -> bool {
    float_neutral(hm::clamp_ratio_threshold(float_in(&row["x"]))) == row["r"]
}

fn check_weak(row: &Value) -> bool {
    let applied = i64_of(&row["applied"]);
    let explicit = i64_of(&row["explicit"]);
    let t = float_in(&row["t"]);
    let mut stats = hm::MemoryStats::new(
        unicode::cps("m"),
        Vec::new(),
        Vec::new(),
        DateTime::new(2026, 3, 1, 12, 0, 0, 0, Some(0)).expect("D1"),
        DateTime::new(2026, 3, 1, 12, 0, 0, 0, Some(0)).expect("D1"),
    );
    stats.applied_count = applied;
    stats.explicit_applied_count = explicit;
    json!(hm::weakly_endorsed(applied, explicit, t)) == row["r"]
        && json!(hm::is_weakly_endorsed(&stats, t)) == row["rs"]
}

fn check_unread(row: &Value) -> bool {
    let items: Vec<(PyStr, HistoryUnread)> = row["in"]
        .as_array()
        .expect("items")
        .iter()
        .map(|p| (text_field(&p[0], "v"), cause(p[1].as_str().expect("cause"))))
        .collect();
    to_neutral(&hm::unread_fields(&items).to_dict()) == row["d"]
}

fn check_unread_dict(row: &Value) -> bool {
    let items = hm::unread_dict(
        i64_of(&row["unread"]),
        &pairs(row.get("causes")),
        &texts_of(row.get("ids")),
    );
    let d = PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    );
    to_neutral(&d) == row["d"]
}

fn check_actors(row: &Value) -> bool {
    let i = &row["in"];
    let mut a = hm::ActorSlices::default();
    if let Some(d) = i.get("declared") {
        a.declared = d
            .as_array()
            .expect("declared")
            .iter()
            .map(actor_of)
            .collect();
    }
    if let Some(u) = i.get("undeclared") {
        a.undeclared = actor_of(u);
    }
    to_neutral(&a.to_dict()) == row["d"]
}

fn check_vdebt(row: &Value) -> bool {
    let i = &row["in"];
    let mut v = hm::VerificationDebt::new(i64_of(&i["stale_after_days"]));
    v.never_verified = memories(i.get("never_verified"));
    v.never_verified_total = int_or_zero(i, "never_verified_total");
    v.stale = memories(i.get("stale"));
    v.stale_total = int_or_zero(i, "stale_total");
    v.fresh_count = int_or_zero(i, "fresh_count");
    v.never_verified_checkable = int_or_zero(i, "never_verified_checkable");
    v.stale_checkable = int_or_zero(i, "stale_checkable");
    answer(v.to_dict()) == row["d"]
}

fn check_drow(row: &Value) -> bool {
    to_neutral(&drift_row_of(&row["in"]).to_dict()) == row["d"]
}

fn check_ddebt(row: &Value) -> bool {
    let i = &row["in"];
    let mut d = hm::CommitDriftDebt::new(opt_text(i, "current_repo"), opt_text(i, "current_cwd"));
    d.rows = drift_rows(i.get("rows"));
    d.total_drifted = int_or_zero(i, "total_drifted");
    d.unconfirmed = int_or_zero(i, "unconfirmed");
    d.unread = int_or_zero(i, "unread");
    d.unread_causes = pairs(i.get("unread_causes"));
    d.unread_ids = texts_of(i.get("unread_ids"));
    to_neutral(&d.to_dict()) == row["d"]
}

fn check_group(row: &Value) -> bool {
    to_neutral(&group_of(&row["in"]).to_dict()) == row["d"]
}

fn check_cross(row: &Value) -> bool {
    let i = &row["in"];
    let mut c = hm::CrossRepoDrift::default();
    if let Some(g) = i.get("groups") {
        c.groups = g.as_array().expect("groups").iter().map(group_of).collect();
    }
    if let Some(s) = i.get("skipped") {
        let PyValue::List(ref items) = neutral(s) else {
            panic!("a skipped list")
        };
        c.skipped = items.clone();
    }
    c.total_drifted = int_or_zero(i, "total_drifted");
    c.unanchorable = int_or_zero(i, "unanchorable");
    to_neutral(&c.to_dict()) == row["d"]
}

fn check_cold(row: &Value) -> bool {
    let i = &row["in"];
    let mut c = hm::ColdEndorsementMemories::new(i64_of(&i["min_retrievals"]));
    c.rows = memories(i.get("rows"));
    c.total = int_or_zero(i, "total");
    answer(c.to_dict()) == row["d"]
}

fn check_silent(row: &Value) -> bool {
    let i = &row["in"];
    let s = hm::SilentMissStats {
        audited_total: int_or_zero(i, "audited_total"),
        miss_total: int_or_zero(i, "miss_total"),
        unique_miss_memories: int_or_zero(i, "unique_miss_memories"),
        no_signal_total: int_or_zero(i, "no_signal_total"),
    };
    to_neutral(&s.to_dict()) == row["d"]
}

fn check_recent(row: &Value) -> bool {
    let i = &row["in"];
    let r = hm::RecentSilentMiss {
        event_id: opt_text(i, "event_id"),
        top_hit_id: opt_text(i, "top_hit_id"),
        query_preview: opt_text(i, "query_preview"),
        ts: opt_text(i, "ts"),
    };
    to_neutral(&r.to_dict()) == row["d"]
}

// ---------------------------------------------------------------------------
// The sweeps, drawn by the generator's rule.
// ---------------------------------------------------------------------------

struct Draw(u64);

impl Draw {
    fn nxt(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 33
    }

    fn pick(&mut self, n: u64) -> u64 {
        self.nxt() % n
    }

    fn pick_i(&mut self, n: u64) -> i64 {
        self.pick(n) as i64
    }

    fn pick_u(&mut self, n: usize) -> usize {
        self.pick(n as u64) as usize
    }
}

const BIG: [i64; 7] = [(1 << 53) + 1, i64::MAX, -1, i64::MIN, 1 << 53, 1999, 3];
const KINDS: [&str; 4] = ["update", "verify", "contradicted", "corrected"];
const CAUSES: [&str; 6] = [
    "timeout",
    "over_budget",
    "shallow",
    "error",
    "unconfirmed_cut",
    "call_budget",
];

fn texts() -> Vec<PyStr> {
    let mut v: Vec<PyStr> = [
        "",
        "a",
        "global",
        "projects:x",
        "caf\u{e9}",
        "\u{2014}",
        "\u{1f600}",
    ]
    .iter()
    .map(|s| unicode::cps(s))
    .collect();
    v.push(vec![u32::from(b's'), 0xD800]);
    v.push(unicode::cps("q\"\\\n"));
    v
}

fn draw_dt(d: &mut Draw, naive_ok: bool) -> DateTime {
    let year = 1 + d.pick_i(9999);
    let month = 1 + d.pick(12) as u32;
    let day = 1 + d.pick(28) as u32;
    let hour = d.pick(24) as u32;
    let minute = d.pick(60) as u32;
    let second = d.pick(60) as u32;
    let micro = if d.pick(2) != 0 {
        d.pick(1_000_000) as u32
    } else {
        0
    };
    let offset = match d.pick(8) {
        0 => (!naive_ok).then_some(0),
        1 => Some(0),
        2..=5 => Some((d.pick_i(2879) - 1439) * 60_000_000),
        6 => Some((d.pick_i(172_799) - 86_399) * 1_000_000),
        _ => Some(d.pick_i(1 << 31) - (1 << 30)),
    };
    DateTime::new(year, month, day, hour, minute, second, micro, offset).expect("a datetime")
}

fn draw_count(d: &mut Draw) -> i64 {
    if d.pick(4) != 0 {
        return d.pick_i(6);
    }
    BIG[d.pick_u(BIG.len())]
}

fn draw_opt_dt(d: &mut Draw, naive_ok: bool) -> Option<DateTime> {
    if d.pick(3) == 0 {
        return None;
    }
    Some(draw_dt(d, naive_ok))
}

fn draw_text(d: &mut Draw, t: &[PyStr]) -> PyStr {
    t[d.pick_u(t.len())].clone()
}

fn draw_opt_text(d: &mut Draw, t: &[PyStr]) -> Option<PyStr> {
    if d.pick(3) == 0 {
        return None;
    }
    Some(draw_text(d, t))
}

/// Unit 1's `draw_memory`.
fn draw_memory(d: &mut Draw, i: usize, t: &[PyStr]) -> hm::MemoryStats {
    let naive = d.pick(16) == 0;
    let id = unicode::cps(&format!("m{i}"));
    let n = d.pick(4);
    let scopes = (0..n).map(|_| draw_text(d, t)).collect();
    let summary = draw_text(d, t);
    let created = draw_dt(d, naive);
    let updated = draw_dt(d, naive);
    let mut m = hm::MemoryStats::new(id, scopes, summary, created, updated);
    for name in COUNTS {
        *count_slot(&mut m, name) = draw_count(d);
    }
    m.last_used_at = draw_opt_dt(d, naive);
    m.last_contradicted_at = draw_opt_dt(d, naive);
    m.last_verified_at = draw_opt_dt(d, naive);
    m.last_corroborated = draw_opt_dt(d, naive);
    let cat = d.pick_u(4);
    m.category = (cat != 0).then(|| Category::MEMBERS[cat - 1].1);
    for _ in 0..d.pick(3) {
        let kind = PyValue::str(KINDS[d.pick_u(4)]);
        let ts = PyValue::str(&draw_dt(d, false).isoformat());
        let note = if d.pick(2) != 0 {
            PyValue::None
        } else {
            PyValue::Str(draw_text(d, t))
        };
        m.resolution_timeline.push(PyValue::Dict(vec![
            (PyValue::str("kind"), kind),
            (PyValue::str("ts"), ts),
            (PyValue::str("note"), note),
        ]));
    }
    m
}

fn draw_memories(d: &mut Draw, i: usize, t: &[PyStr]) -> Vec<hm::MemoryStats> {
    (0..d.pick(3)).map(|_| draw_memory(d, i, t)).collect()
}

fn draw_census(d: &mut Draw, names: &[PyStr]) -> Vec<(PyStr, i64)> {
    let n = d.pick_u(3);
    let start = d.pick_u(names.len());
    (0..n)
        .map(|j| (names[(start + j) % names.len()].clone(), draw_count(d)))
        .collect()
}

fn draw_actor(d: &mut Draw, undeclared: bool, t: &[PyStr]) -> hm::ActorSlice {
    let client = if undeclared {
        None
    } else {
        draw_opt_text(d, t)
    };
    hm::ActorSlice {
        client,
        memories: draw_count(d),
        memories_in_window: draw_count(d),
        events: draw_count(d),
        searches: draw_count(d),
        applies: draw_count(d),
        models: draw_census(d, t),
        principals: draw_census(d, t),
    }
}

fn draw_drift_row(d: &mut Draw, t: &[PyStr]) -> hm::CommitDriftRow {
    let id = draw_text(d, t);
    let scopes = (0..d.pick(3)).map(|_| draw_text(d, t)).collect();
    let summary = draw_text(d, t);
    let verified = draw_opt_dt(d, true);
    let commits = draw_count(d);
    let mut row = hm::CommitDriftRow::new(id, scopes, summary, verified, commits);
    match d.pick(3) {
        1 => row.basis = unicode::cps("reachability"),
        2 => row.basis = draw_text(d, t),
        _ => {}
    }
    row
}

fn draw_drift_rows(d: &mut Draw, t: &[PyStr]) -> Vec<hm::CommitDriftRow> {
    (0..d.pick(3)).map(|_| draw_drift_row(d, t)).collect()
}

fn draw_unread(d: &mut Draw, t: &[PyStr]) -> hm::UnreadFields {
    if d.pick(2) != 0 {
        let n = d.pick(26);
        let m: Vec<(PyStr, HistoryUnread)> = (0..n)
            .map(|j| {
                let id = unicode::cps(&format!("u{}", n - j));
                (id, cause(CAUSES[d.pick_u(6)]))
            })
            .collect();
        return hm::unread_fields(&m);
    }
    let unread = draw_count(d);
    let names: Vec<PyStr> = CAUSES.iter().map(|c| unicode::cps(c)).collect();
    let unread_causes = draw_census(d, &names);
    let unread_ids = (0..d.pick(3)).map(|_| draw_text(d, t)).collect();
    hm::UnreadFields {
        unread,
        unread_causes,
        unread_ids,
    }
}

fn draw_group(d: &mut Draw, t: &[PyStr]) -> hm::CrossRepoDriftGroup {
    let repo = draw_text(d, t);
    let root = draw_text(d, t);
    let mut g = hm::CrossRepoDriftGroup::new(repo, root);
    g.rows = draw_drift_rows(d, t);
    g.total_drifted = draw_count(d);
    g.candidates = draw_count(d);
    let u = draw_unread(d, t);
    g.unread = u.unread;
    g.unread_causes = u.unread_causes;
    g.unread_ids = u.unread_ids;
    g.last_checked_at = draw_opt_dt(d, true);
    g
}

/// The generator's `draw_rollup`, answered as its `to_dict`.
fn draw_rollup(d: &mut Draw, i: usize, t: &[PyStr]) -> Result<PyValue, HealthModelError> {
    Ok(match d.pick(9) {
        0 => {
            let declared = (0..d.pick(3)).map(|_| draw_actor(d, false, t)).collect();
            let mut a = hm::ActorSlices {
                declared,
                ..Default::default()
            };
            if d.pick(2) != 0 {
                a.undeclared = draw_actor(d, true, t);
            }
            a.to_dict()
        }
        1 => {
            let mut v = hm::VerificationDebt::new(draw_count(d));
            v.never_verified = draw_memories(d, i, t);
            v.never_verified_total = draw_count(d);
            v.stale = draw_memories(d, i, t);
            v.stale_total = draw_count(d);
            v.fresh_count = draw_count(d);
            v.never_verified_checkable = draw_count(d);
            v.stale_checkable = draw_count(d);
            return v.to_dict();
        }
        2 => draw_drift_row(d, t).to_dict(),
        3 => {
            let repo = draw_opt_text(d, t);
            let cwd = draw_opt_text(d, t);
            let mut c = hm::CommitDriftDebt::new(repo, cwd);
            c.rows = draw_drift_rows(d, t);
            c.total_drifted = draw_count(d);
            c.unconfirmed = draw_count(d);
            let u = draw_unread(d, t);
            c.unread = u.unread;
            c.unread_causes = u.unread_causes;
            c.unread_ids = u.unread_ids;
            c.to_dict()
        }
        4 => draw_group(d, t).to_dict(),
        5 => {
            let groups = (0..d.pick(3)).map(|_| draw_group(d, t)).collect();
            let skipped = (0..d.pick(3))
                .map(|_| {
                    let repo = PyValue::Str(draw_text(d, t));
                    let reason = PyValue::Str(draw_text(d, t));
                    PyValue::Dict(vec![
                        (PyValue::str("repo"), repo),
                        (PyValue::str("reason"), reason),
                    ])
                })
                .collect();
            hm::CrossRepoDrift {
                groups,
                skipped,
                total_drifted: draw_count(d),
                unanchorable: draw_count(d),
            }
            .to_dict()
        }
        6 => {
            let mut c = hm::ColdEndorsementMemories::new(draw_count(d));
            c.rows = draw_memories(d, i, t);
            c.total = draw_count(d);
            return c.to_dict();
        }
        7 => hm::SilentMissStats {
            audited_total: draw_count(d),
            miss_total: draw_count(d),
            unique_miss_memories: draw_count(d),
            no_signal_total: draw_count(d),
        }
        .to_dict(),
        _ => hm::RecentSilentMiss {
            event_id: draw_opt_text(d, t),
            top_hit_id: draw_opt_text(d, t),
            query_preview: draw_opt_text(d, t),
            ts: draw_opt_text(d, t),
        }
        .to_dict(),
    })
}

fn ascii(text: &[u32]) -> String {
    text.iter()
        .map(|&c| char::from_u32(c).filter(char::is_ascii).expect("ASCII"))
        .collect()
}

fn digest(lines: &[String]) -> String {
    let h = Sha256::digest(lines.join("\n").as_bytes());
    h.iter().map(|b| format!("{b:02x}")).collect()
}

fn rollup_sweep(seed: u64, count: usize) -> (String, usize) {
    let t = texts();
    let mut d = Draw(seed);
    let mut lines = Vec::with_capacity(count);
    let mut raised = 0;
    for i in 0..count {
        lines.push(match draw_rollup(&mut d, i, &t) {
            Ok(v) => ascii(&json::dumps(&v, DumpOptions::DEFAULT).expect("dumps")),
            Err(e) => {
                raised += 1;
                e.name().to_owned()
            }
        });
    }
    (digest(&lines), raised)
}

/// Unit 1's `draw_float`.
fn draw_float(d: &mut Draw) -> f64 {
    match d.pick(4) {
        0 => {
            let a = d.nxt();
            let b = d.nxt();
            let c = d.nxt();
            f64::from_bits((a << 33) | (b << 2) | (c & 3))
        }
        1 => {
            let n = d.pick(5000);
            let m = 1 + d.pick(5000);
            n as f64 / m as f64
        }
        2 => {
            let mid = (2 * d.pick(1000) + 1) as f64 / 2000.0;
            let step = d.pick_i(3) - 1;
            f64::from_bits(mid.to_bits().wrapping_add_signed(step))
        }
        _ => {
            let n = d.pick_i(1 << 31) - (1 << 30);
            let e = d.pick(40) as i32;
            n as f64 / 2f64.powi(e)
        }
    }
}

fn pred_sweep(seed: u64, count: usize) -> String {
    let mut d = Draw(seed);
    let mut lines = Vec::with_capacity(count);
    for _ in 0..count {
        let applied = draw_count(&mut d);
        let explicit = draw_count(&mut d);
        let t = draw_float(&mut d);
        let c = hm::clamp_ratio_threshold(t);
        let w = u8::from(hm::weakly_endorsed(applied, explicit, t));
        let wc = u8::from(hm::weakly_endorsed(applied, explicit, c));
        lines.push(format!(
            "{applied} {explicit} {:016x} {:016x} {w} {wc}",
            t.to_bits(),
            c.to_bits()
        ));
    }
    digest(&lines)
}

#[test]
fn health_model_debt_every_row() {
    let lines = jsonl(NAME);
    let header = &lines[0];
    let count = header["count"].as_u64().expect("count") as usize;
    assert_eq!(lines.len(), count + 3, "{NAME}: header, rows, two sweeps");
    let caps = &header["caps"];
    assert_eq!(caps["verification_debt"], json!(hm::VERIFICATION_DEBT_CAP));
    assert_eq!(
        caps["cold_endorsement_min_retrievals"],
        json!(hm::COLD_ENDORSEMENT_MIN_RETRIEVALS)
    );
    assert_eq!(caps["cold_endorsement"], json!(hm::COLD_ENDORSEMENT_CAP));
    assert_eq!(
        caps["cross_repo_max_roots"],
        json!(hm::CROSS_REPO_MAX_ROOTS)
    );
    assert_eq!(caps["commit_drift_debt"], json!(hm::COMMIT_DRIFT_DEBT_CAP));
    assert_eq!(
        caps["recent_silent_misses"],
        json!(hm::RECENT_SILENT_MISSES_CAP)
    );
    let row = hm::CommitDriftRow::new(Vec::new(), Vec::new(), Vec::new(), None, 0);
    assert_eq!(text_field(header, "basis_default"), row.basis);
    let mut failures = Vec::new();
    let mut checked = 0;
    for (i, row) in lines[1..=count].iter().enumerate() {
        let ok = match row["k"].as_str().expect("kind") {
            "clamp" => check_clamp(row),
            "weak" => check_weak(row),
            "unread" => check_unread(row),
            "unread_dict" => check_unread_dict(row),
            "actors" => check_actors(row),
            "vdebt" => check_vdebt(row),
            "drow" => check_drow(row),
            "ddebt" => check_ddebt(row),
            "group" => check_group(row),
            "cross" => check_cross(row),
            "cold" => check_cold(row),
            "silent" => check_silent(row),
            "recent" => check_recent(row),
            other => panic!("unknown kind {other}"),
        };
        if !ok {
            failures.push(format!("row {i}: {row}"));
        }
        checked += 1;
    }
    assert!(
        failures.is_empty(),
        "{} rows differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_count(NAME, checked, count);
}

#[test]
fn health_model_debt_sweeps() {
    let lines = jsonl(NAME);
    let count = lines[0]["count"].as_u64().expect("count") as usize;
    let sweeps = &lines[count + 1..];
    let kinds: Vec<&str> = sweeps.iter().map(|s| s["k"].as_str().expect("k")).collect();
    assert_eq!(kinds, ["rollup", "pred"]);
    for s in sweeps {
        let seed = s["seed"].as_u64().expect("seed");
        let n = s["count"].as_u64().expect("count") as usize;
        assert!(n > 0, "an empty sweep");
        let got = if s["k"] == "rollup" {
            let (digest, raised) = rollup_sweep(seed, n);
            assert!(raised > 0 && raised < n, "the sweep raises on some rows");
            digest
        } else {
            pred_sweep(seed, n)
        };
        assert_eq!(got, s["sha256"].as_str().expect("sha256"), "sweep {s}");
    }
}
