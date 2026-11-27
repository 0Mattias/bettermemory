//! Every row of `health_model_report.jsonl` against unit health_model-3:
//! `Recommendation`, `ProvenanceRow`, `ProvenanceDebt` and `HealthReport`
//! with their `to_dict`, the caps and `RECOMMENDATION_KINDS`; and the sweep
//! of drawn reports by count and digest, drawn here by the generator's rule
//! (`draw_report` over the pools of units 1 and 2). The readers and draws
//! of units 1 and 2 are those of `golden_health_model_debt.rs`.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::float_repr;
use bettermemory_core::health_model::{self as hm, HealthModelError};
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::origin_history::HistoryUnread;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::Category;
use bettermemory_core::store::journal::EpisodeVolume;
use bettermemory_core::unicode;
use common::{assert_count, jsonl, neutral, text_field};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const NAME: &str = "health_model_report.jsonl";

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

fn ascii(text: &[u32]) -> String {
    text.iter()
        .map(|&c| char::from_u32(c).filter(char::is_ascii).expect("ASCII"))
        .collect()
}

fn digest(lines: &[String]) -> String {
    let h = Sha256::digest(lines.join("\n").as_bytes());
    h.iter().map(|b| format!("{b:02x}")).collect()
}

// ---------------------------------------------------------------------------
// Unit 3's readers.
// ---------------------------------------------------------------------------

/// Whether a text field is spelled, as text or as code points.
fn has_text(i: &Value, name: &str) -> bool {
    i.get(name).is_some() || i.get(format!("{name}_cps")).is_some()
}

fn rec_of(i: &Value) -> hm::Recommendation {
    let mut r = hm::Recommendation::new(
        text_field(i, "kind"),
        text_field(i, "summary"),
        text_field(i, "action"),
        i64_of(&i["count"]),
    );
    r.memory_ids = texts_of(i.get("memory_ids"));
    if has_text(i, "scope") {
        r.scope = opt_text(i, "scope");
    }
    r
}

fn prow_of(i: &Value) -> hm::ProvenanceRow {
    hm::ProvenanceRow {
        id: text_field(i, "id"),
        scopes: texts_of(i.get("scopes")),
        summary: text_field(i, "summary"),
        created: dt(&i["created"]),
    }
}

fn pdebt_of(i: &Value) -> hm::ProvenanceDebt {
    let mut p = hm::ProvenanceDebt::new(pairs(i.get("counts")), i64_of(&i["unaccounted_total"]));
    if let Some(rows) = i.get("unaccounted") {
        p.unaccounted = rows.as_array().expect("rows").iter().map(prow_of).collect();
    }
    p
}

fn list_of<T>(v: Option<&Value>, f: impl Fn(&Value) -> T) -> Vec<T> {
    v.map_or_else(Vec::new, |v| {
        v.as_array().expect("a list").iter().map(f).collect()
    })
}

/// An optional bucket: absent keeps the default (None), null is None.
fn optional<T>(i: &Value, name: &str, f: impl Fn(&Value) -> T) -> Option<Option<T>> {
    i.get(name).map(|v| (!v.is_null()).then(|| f(v)))
}

fn marker_of(i: &Value) -> hm::MarkerStats {
    hm::MarkerStats {
        marker: text_field(i, "marker"),
        fire_count: int_or_zero(i, "fire_count"),
        override_count: int_or_zero(i, "override_count"),
    }
}

fn scope_of(i: &Value) -> hm::ScopeHealth {
    hm::ScopeHealth {
        scope: text_field(i, "scope"),
        active: int_or_zero(i, "active"),
        dead: int_or_zero(i, "dead"),
        cold: int_or_zero(i, "cold"),
        contradicted: int_or_zero(i, "contradicted"),
        applied_total: int_or_zero(i, "applied_total"),
    }
}

fn actors_of(i: &Value) -> hm::ActorSlices {
    let mut a = hm::ActorSlices {
        declared: list_of(i.get("declared"), actor_of),
        ..Default::default()
    };
    if let Some(u) = i.get("undeclared") {
        a.undeclared = actor_of(u);
    }
    a
}

fn vdebt_of(i: &Value) -> hm::VerificationDebt {
    let mut v = hm::VerificationDebt::new(i64_of(&i["stale_after_days"]));
    v.never_verified = memories(i.get("never_verified"));
    v.never_verified_total = int_or_zero(i, "never_verified_total");
    v.stale = memories(i.get("stale"));
    v.stale_total = int_or_zero(i, "stale_total");
    v.fresh_count = int_or_zero(i, "fresh_count");
    v.never_verified_checkable = int_or_zero(i, "never_verified_checkable");
    v.stale_checkable = int_or_zero(i, "stale_checkable");
    v
}

fn ddebt_of(i: &Value) -> hm::CommitDriftDebt {
    let mut d = hm::CommitDriftDebt::new(opt_text(i, "current_repo"), opt_text(i, "current_cwd"));
    d.rows = drift_rows(i.get("rows"));
    d.total_drifted = int_or_zero(i, "total_drifted");
    d.unconfirmed = int_or_zero(i, "unconfirmed");
    d.unread = int_or_zero(i, "unread");
    d.unread_causes = pairs(i.get("unread_causes"));
    d.unread_ids = texts_of(i.get("unread_ids"));
    d
}

fn cross_of(i: &Value) -> hm::CrossRepoDrift {
    let mut c = hm::CrossRepoDrift {
        groups: list_of(i.get("groups"), group_of),
        ..Default::default()
    };
    if let Some(s) = i.get("skipped") {
        let PyValue::List(ref items) = neutral(s) else {
            panic!("a skipped list")
        };
        c.skipped = items.clone();
    }
    c.total_drifted = int_or_zero(i, "total_drifted");
    c.unanchorable = int_or_zero(i, "unanchorable");
    c
}

fn silent_of(i: &Value) -> hm::SilentMissStats {
    hm::SilentMissStats {
        audited_total: int_or_zero(i, "audited_total"),
        miss_total: int_or_zero(i, "miss_total"),
        unique_miss_memories: int_or_zero(i, "unique_miss_memories"),
        no_signal_total: int_or_zero(i, "no_signal_total"),
    }
}

fn recent_of(i: &Value) -> hm::RecentSilentMiss {
    hm::RecentSilentMiss {
        event_id: opt_text(i, "event_id"),
        top_hit_id: opt_text(i, "top_hit_id"),
        query_preview: opt_text(i, "query_preview"),
        ts: opt_text(i, "ts"),
    }
}

fn cold_of(i: &Value) -> hm::ColdEndorsementMemories {
    let mut c = hm::ColdEndorsementMemories::new(i64_of(&i["min_retrievals"]));
    c.rows = memories(i.get("rows"));
    c.total = int_or_zero(i, "total");
    c
}

fn coverage_of(i: &Value) -> hm::TelemetryCoverage {
    let mut c = hm::TelemetryCoverage::new(
        i64_of(&i["hook_telemetry_events"]),
        i["covered"].as_bool().expect("covered"),
        i["dead_weight_suppressed"].as_bool().expect("suppressed"),
    );
    if let Some(b) = i.get("cold_endorsement_suppressed") {
        c.cold_endorsement_suppressed = b.as_bool().expect("bool");
    }
    if has_text(i, "reason") {
        c.reason = opt_text(i, "reason");
    }
    c
}

fn volume_of(i: &Value) -> EpisodeVolume {
    EpisodeVolume {
        sessions: i64_of(&i["sessions"]),
        episodes: i64_of(&i["episodes"]),
        bytes: i64_of(&i["bytes"]),
        prunable_sessions: i64_of(&i["prunable_sessions"]),
        ttl_days: i64_of(&i["ttl_days"]),
    }
}

/// A `HealthReport` from the fields a row sets; the rest keep the
/// constructor's defaults.
fn report_of(i: &Value) -> hm::HealthReport {
    let mut r = hm::HealthReport::new(
        dt(&i["generated_at"]),
        i64_of(&i["window_days"]),
        i64_of(&i["total_active_memories"]),
        i64_of(&i["total_events"]),
        i64_of(&i["distinct_sessions"]),
    );
    let known = [
        "generated_at",
        "window_days",
        "total_active_memories",
        "total_events",
        "distinct_sessions",
        "dead_weight",
        "cold_memories",
        "heavily_used",
        "contradicted",
        "marker_stats",
        "scope_distribution",
        "scope_health",
        "rare_scopes",
        "actor_slices",
        "orphan_use_events",
        "verification_debt",
        "commit_drift_debt",
        "cross_repo_drift",
        "provenance",
        "silent_misses",
        "recent_silent_misses",
        "cold_endorsement_memories",
        "recommendations",
        "telemetry_coverage",
        "episode_volume",
    ];
    for k in i.as_object().expect("fields").keys() {
        assert!(known.contains(&k.as_str()), "unknown field {k}");
    }
    r.dead_weight = memories(i.get("dead_weight"));
    r.cold_memories = memories(i.get("cold_memories"));
    r.heavily_used = memories(i.get("heavily_used"));
    r.contradicted = memories(i.get("contradicted"));
    r.marker_stats = list_of(i.get("marker_stats"), marker_of);
    r.scope_distribution = pairs(i.get("scope_distribution"));
    r.scope_health = list_of(i.get("scope_health"), scope_of);
    r.rare_scopes = texts_of(i.get("rare_scopes"));
    if let Some(a) = i.get("actor_slices") {
        r.actor_slices = actors_of(a);
    }
    r.orphan_use_events = int_or_zero(i, "orphan_use_events");
    if let Some(v) = i.get("verification_debt") {
        r.verification_debt = vdebt_of(v);
    }
    if let Some(v) = optional(i, "commit_drift_debt", ddebt_of) {
        r.commit_drift_debt = v;
    }
    if let Some(v) = optional(i, "cross_repo_drift", cross_of) {
        r.cross_repo_drift = v;
    }
    if let Some(v) = optional(i, "provenance", pdebt_of) {
        r.provenance = v;
    }
    if let Some(v) = i.get("silent_misses") {
        r.silent_misses = silent_of(v);
    }
    r.recent_silent_misses = list_of(i.get("recent_silent_misses"), recent_of);
    if let Some(v) = i.get("cold_endorsement_memories") {
        r.cold_endorsement_memories = cold_of(v);
    }
    r.recommendations = list_of(i.get("recommendations"), rec_of);
    if let Some(v) = optional(i, "telemetry_coverage", coverage_of) {
        r.telemetry_coverage = v;
    }
    if let Some(v) = optional(i, "episode_volume", volume_of) {
        r.episode_volume = v;
    }
    r
}

// ---------------------------------------------------------------------------
// Unit 3's draws, in the generator's order.
// ---------------------------------------------------------------------------

/// Unit 1's `draw_marker`.
fn draw_marker(d: &mut Draw, t: &[PyStr]) -> hm::MarkerStats {
    let marker = draw_text(d, t);
    let fire_count = draw_count(d);
    let override_count = draw_count(d);
    hm::MarkerStats {
        marker,
        fire_count,
        override_count,
    }
}

fn draw_prov_row(d: &mut Draw, t: &[PyStr]) -> hm::ProvenanceRow {
    let id = draw_text(d, t);
    let scopes = (0..d.pick(3)).map(|_| draw_text(d, t)).collect();
    let summary = draw_text(d, t);
    let created = draw_dt(d, true);
    hm::ProvenanceRow {
        id,
        scopes,
        summary,
        created,
    }
}

fn draw_rec(d: &mut Draw, t: &[PyStr]) -> hm::Recommendation {
    let kind = unicode::cps(hm::RECOMMENDATION_KINDS[d.pick_u(hm::RECOMMENDATION_KINDS.len())]);
    let summary = draw_text(d, t);
    let action = draw_text(d, t);
    let mut r = hm::Recommendation::new(kind, summary, action, draw_count(d));
    r.memory_ids = (0..d.pick(3)).map(|_| draw_text(d, t)).collect();
    r.scope = draw_opt_text(d, t);
    r
}

/// The generator's `draw_report`.
fn draw_report(d: &mut Draw, i: usize, t: &[PyStr]) -> hm::HealthReport {
    let generated_at = draw_dt(d, true);
    let mut r = hm::HealthReport::new(
        generated_at,
        draw_count(d),
        draw_count(d),
        draw_count(d),
        draw_count(d),
    );
    r.dead_weight = draw_memories(d, i, t);
    r.cold_memories = draw_memories(d, i, t);
    r.heavily_used = draw_memories(d, i, t);
    r.contradicted = draw_memories(d, i, t);
    r.marker_stats = (0..d.pick(3)).map(|_| draw_marker(d, t)).collect();
    r.scope_distribution = draw_census(d, t);
    for _ in 0..d.pick(3) {
        let scope = draw_text(d, t);
        r.scope_health.push(hm::ScopeHealth {
            scope,
            active: draw_count(d),
            dead: draw_count(d),
            cold: draw_count(d),
            contradicted: draw_count(d),
            applied_total: draw_count(d),
        });
    }
    r.rare_scopes = (0..d.pick(3)).map(|_| draw_text(d, t)).collect();
    if d.pick(2) != 0 {
        let declared = (0..d.pick(3)).map(|_| draw_actor(d, false, t)).collect();
        r.actor_slices = hm::ActorSlices {
            declared,
            undeclared: draw_actor(d, true, t),
        };
    }
    r.orphan_use_events = draw_count(d);
    if d.pick(2) != 0 {
        let mut v = hm::VerificationDebt::new(draw_count(d));
        v.never_verified = draw_memories(d, i, t);
        v.never_verified_total = draw_count(d);
        v.stale = draw_memories(d, i, t);
        v.stale_total = draw_count(d);
        v.fresh_count = draw_count(d);
        v.never_verified_checkable = draw_count(d);
        v.stale_checkable = draw_count(d);
        r.verification_debt = v;
    }
    if d.pick(2) != 0 {
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
        r.commit_drift_debt = Some(c);
    }
    if d.pick(2) != 0 {
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
        r.cross_repo_drift = Some(hm::CrossRepoDrift {
            groups,
            skipped,
            total_drifted: draw_count(d),
            unanchorable: draw_count(d),
        });
    }
    if d.pick(2) != 0 {
        let counts = draw_census(d, t);
        let mut p = hm::ProvenanceDebt::new(counts, draw_count(d));
        p.unaccounted = (0..d.pick(3)).map(|_| draw_prov_row(d, t)).collect();
        r.provenance = Some(p);
    }
    if d.pick(2) != 0 {
        r.silent_misses = hm::SilentMissStats {
            audited_total: draw_count(d),
            miss_total: draw_count(d),
            unique_miss_memories: draw_count(d),
            no_signal_total: draw_count(d),
        };
    }
    for _ in 0..d.pick(3) {
        r.recent_silent_misses.push(hm::RecentSilentMiss {
            event_id: draw_opt_text(d, t),
            top_hit_id: draw_opt_text(d, t),
            query_preview: draw_opt_text(d, t),
            ts: draw_opt_text(d, t),
        });
    }
    if d.pick(2) != 0 {
        let mut c = hm::ColdEndorsementMemories::new(draw_count(d));
        c.rows = draw_memories(d, i, t);
        c.total = draw_count(d);
        r.cold_endorsement_memories = c;
    }
    r.recommendations = (0..d.pick(3)).map(|_| draw_rec(d, t)).collect();
    if d.pick(2) != 0 {
        let events = draw_count(d);
        let covered = d.pick(2) != 0;
        let dead = d.pick(2) != 0;
        let mut c = hm::TelemetryCoverage::new(events, covered, dead);
        c.cold_endorsement_suppressed = d.pick(2) != 0;
        c.reason = draw_opt_text(d, t);
        r.telemetry_coverage = Some(c);
    }
    if d.pick(2) != 0 {
        r.episode_volume = Some(EpisodeVolume {
            sessions: draw_count(d),
            episodes: draw_count(d),
            bytes: draw_count(d),
            prunable_sessions: draw_count(d),
            ttl_days: draw_count(d),
        });
    }
    r
}

fn report_sweep(seed: u64, count: usize) -> (String, usize) {
    let t = texts();
    let mut d = Draw(seed);
    let mut lines = Vec::with_capacity(count);
    let mut raised = 0;
    for i in 0..count {
        lines.push(match draw_report(&mut d, i, &t).to_dict() {
            Ok(v) => ascii(&json::dumps(&v, DumpOptions::DEFAULT).expect("dumps")),
            Err(e) => {
                raised += 1;
                e.name().to_owned()
            }
        });
    }
    (digest(&lines), raised)
}

#[test]
fn health_model_report_every_row() {
    let lines = jsonl(NAME);
    let header = &lines[0];
    let count = header["count"].as_u64().expect("count") as usize;
    assert_eq!(lines.len(), count + 2, "{NAME}: header, rows, the sweep");
    let caps = &header["caps"];
    assert_eq!(
        caps["recommendation_row"],
        json!(hm::RECOMMENDATION_ROW_CAP)
    );
    assert_eq!(
        caps["recommendation_size_floor"],
        json!(hm::RECOMMENDATION_SIZE_FLOOR)
    );
    assert_eq!(caps["provenance_row"], json!(hm::PROVENANCE_ROW_CAP));
    assert_eq!(header["kinds"], json!(hm::RECOMMENDATION_KINDS));
    let mut failures = Vec::new();
    let mut checked = 0;
    let mut raised = 0;
    for (n, row) in lines[1..=count].iter().enumerate() {
        let i = &row["in"];
        let got = match row["k"].as_str().expect("kind") {
            "rec" => to_neutral(&rec_of(i).to_dict()),
            "prow" => to_neutral(&prow_of(i).to_dict()),
            "pdebt" => to_neutral(&pdebt_of(i).to_dict()),
            "report" => answer(report_of(i).to_dict()),
            other => panic!("unknown kind {other}"),
        };
        if row["d"].get("x").is_some() {
            raised += 1;
        }
        if got != row["d"] {
            failures.push(format!("row {n}: {row}\n got {got}"));
        }
        checked += 1;
    }
    assert!(
        failures.is_empty(),
        "{} rows differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert!(raised > 0, "the file pins the raising rows");
    assert_count(NAME, checked, count);
}

#[test]
fn health_model_report_sweep() {
    let lines = jsonl(NAME);
    let count = lines[0]["count"].as_u64().expect("count") as usize;
    let s = &lines[count + 1];
    assert_eq!(s["k"], "report");
    let n = s["count"].as_u64().expect("count") as usize;
    assert!(n > 0, "an empty sweep");
    let (got, raised) = report_sweep(s["seed"].as_u64().expect("seed"), n);
    assert!(raised > 0 && raised < n, "the sweep raises on some reports");
    assert_eq!(got, s["sha256"].as_str().expect("sha256"), "sweep {s}");
}
