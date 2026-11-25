//! Every row of `health_model.jsonl` against `health_model`: `round(x, 3)`,
//! `_freshest_touch_ts`, `_has_unresolved_contradiction`, the event
//! predicates, `_is_dead_weight`, `explicit_ratio`, and each report type's
//! properties and `to_dict`; and the three sweeps by count and digest,
//! their inputs drawn here by the generator's rule.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::float_repr;
use bettermemory_core::health_model::{self as hm, HealthModelError};
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::Category;
use bettermemory_core::search::num::round_digits;
use bettermemory_core::unicode;
use common::{assert_count, jsonl, neutral, text_field};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const NAME: &str = "health_model.jsonl";

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

fn opt_float(f: Option<f64>) -> Value {
    f.map_or(Value::Null, float_neutral)
}

fn float_in(v: &Value) -> f64 {
    match neutral(v) {
        PyValue::Float(f) => f,
        other => panic!("not a float: {other:?}"),
    }
}

/// A datetime by the generator's fields (`_iso`): year to microsecond,
/// then the offset in microseconds or null.
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

fn err(e: HealthModelError) -> Value {
    json!({"x": e.name()})
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

fn check_round(row: &Value) -> bool {
    let x = float_in(&row["x"]);
    float_neutral(round_digits(x, 3)) == row["r"]
}

fn check_ts(row: &Value) -> bool {
    let d: Vec<Option<DateTime>> = row["dts"]
        .as_array()
        .expect("dts")
        .iter()
        .map(opt_dt)
        .collect();
    let got = hm::freshest_touch_ts(
        d[0].as_ref().expect("created"),
        d[1].as_ref().expect("updated"),
        d[2].as_ref(),
        d[3].as_ref(),
    );
    got.map(float_neutral).map_err(err) == Ok(row["r"].clone())
}

fn check_unres(row: &Value) -> bool {
    let got = hm::has_unresolved_contradiction(
        opt_dt(&row["c"]).as_ref(),
        &dt(&row["u"]),
        opt_dt(&row["v"]).as_ref(),
    );
    let got = match got {
        Ok(b) => json!({"r": b}),
        Err(e) => err(e),
    };
    match row.get("x") {
        Some(x) => got == json!({"x": x}),
        None => got == json!({"r": row["r"]}),
    }
}

fn check_ev(row: &Value) -> bool {
    let ev = neutral(&row["ev"]);
    let hook = hm::is_hook_telemetry_event(&ev).map_or_else(err, |b| json!(b));
    let tier = hm::applied_tier(&ev).map_or_else(err, |t| json!(t.value()));
    let etier = hm::endorsement_tier(&ev).map_or_else(err, |t| json!(t.map(|t| t.value())));
    hook == row["hook"] && tier == row["tier"] && etier == row["etier"]
}

fn check_dead(row: &Value) -> bool {
    let a = &row["a"];
    let dw = hm::DeadWeight {
        category: category(&a["category"]),
        freshest_ts: float_in(&a["freshest_ts"]),
        retrieval_count: i64_of(&a["retrieval_count"]),
        applied_count: i64_of(&a["applied_count"]),
        has_unresolved_contradiction: a["has_unresolved_contradiction"].as_bool().expect("bool"),
        earliest_retrieval_ts: (!a["earliest_retrieval_ts"].is_null())
            .then(|| float_in(&a["earliest_retrieval_ts"])),
        cutoff_ts: float_in(&a["cutoff_ts"]),
        grace_cutoff_ts: float_in(&a["grace_cutoff_ts"]),
    };
    json!(dw.is_dead_weight()) == row["r"]
}

fn check_ratio(row: &Value) -> bool {
    let r = hm::explicit_ratio(i64_of(&row["applied"]), i64_of(&row["explicit"]));
    opt_float(r) == row["r"]
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

fn memory_of(i: &Value) -> hm::MemoryStats {
    let scopes = i["scopes"]
        .as_array()
        .expect("scopes")
        .iter()
        .map(|s| text_field(s, "v"))
        .collect();
    let mut m = hm::MemoryStats::new(
        text_field(i, "id"),
        scopes,
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

fn check_memory(row: &Value) -> bool {
    let m = memory_of(&row["in"]);
    let p = &row["props"];
    let unres = m
        .has_unresolved_contradiction()
        .map_or_else(err, |b| json!(b));
    let d = m.to_dict().map_or_else(err, |v| to_neutral(&v));
    unres == p["unres"]
        && opt_float(m.endorsement_ratio()) == p["er"]
        && opt_float(m.model_endorsement_ratio()) == p["mer"]
        && d == row["d"]
}

fn check_marker(row: &Value) -> bool {
    let i = &row["in"];
    let s = hm::MarkerStats {
        marker: text_field(i, "marker"),
        fire_count: i64_of(&i["fire_count"]),
        override_count: i64_of(&i["override_count"]),
    };
    json!(s.total().to_string()) == row["total"]
        && float_neutral(s.override_rate()) == row["rate"]
        && to_neutral(&s.to_dict()) == row["d"]
}

fn int_or_zero(i: &Value, name: &str) -> i64 {
    i.get(name).map_or(0, i64_of)
}

fn check_scope(row: &Value) -> bool {
    let i = &row["in"];
    let s = hm::ScopeHealth {
        scope: text_field(i, "scope"),
        active: int_or_zero(i, "active"),
        dead: int_or_zero(i, "dead"),
        cold: int_or_zero(i, "cold"),
        contradicted: int_or_zero(i, "contradicted"),
        applied_total: int_or_zero(i, "applied_total"),
    };
    to_neutral(&s.to_dict()) == row["d"]
}

fn census(v: Option<&Value>) -> Vec<(PyStr, i64)> {
    v.map_or_else(Vec::new, |v| {
        v.as_array()
            .expect("pairs")
            .iter()
            .map(|p| (text_field(&p[0], "v"), i64_of(&p[1])))
            .collect()
    })
}

fn check_actor(row: &Value) -> bool {
    let i = &row["in"];
    let s = hm::ActorSlice {
        client: (!i.get("client").is_some_and(Value::is_null)).then(|| text_field(i, "client")),
        memories: int_or_zero(i, "memories"),
        memories_in_window: int_or_zero(i, "memories_in_window"),
        events: int_or_zero(i, "events"),
        searches: int_or_zero(i, "searches"),
        applies: int_or_zero(i, "applies"),
        models: census(i.get("models")),
        principals: census(i.get("principals")),
    };
    to_neutral(&s.to_dict()) == row["d"]
}

fn check_coverage(row: &Value) -> bool {
    let i = &row["in"];
    let mut c = hm::TelemetryCoverage::new(
        i64_of(&i["hook_telemetry_events"]),
        i["covered"].as_bool().expect("covered"),
        i["dead_weight_suppressed"].as_bool().expect("suppressed"),
    );
    if let Some(b) = i.get("cold_endorsement_suppressed") {
        c.cold_endorsement_suppressed = b.as_bool().expect("bool");
    }
    if i.get("reason").is_some() || i.get("reason_cps").is_some() {
        c.reason = Some(text_field(i, "reason"));
    }
    to_neutral(&c.to_dict()) == row["d"]
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
}

const I64_MAX: i64 = i64::MAX;
const BIG: [i64; 7] = [(1 << 53) + 1, I64_MAX, -1, i64::MIN, 1 << 53, 1999, 3];
const KINDS: [&str; 4] = ["update", "verify", "contradicted", "corrected"];

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
    let zone = d.pick(8);
    let offset = match zone {
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
    BIG[d.pick(BIG.len() as u64) as usize]
}

fn draw_opt_dt(d: &mut Draw, naive_ok: bool) -> Option<DateTime> {
    if d.pick(3) == 0 {
        return None;
    }
    Some(draw_dt(d, naive_ok))
}

fn pick_text(d: &mut Draw, t: &[PyStr]) -> PyStr {
    t[d.pick(t.len() as u64) as usize].clone()
}

fn draw_memory(d: &mut Draw, i: usize, t: &[PyStr]) -> hm::MemoryStats {
    let naive = d.pick(16) == 0;
    let id = unicode::cps(&format!("m{i}"));
    let n = d.pick(4);
    let scopes = (0..n).map(|_| pick_text(d, t)).collect();
    let summary = pick_text(d, t);
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
    let cat = d.pick(4) as usize;
    m.category = (cat != 0).then(|| Category::MEMBERS[cat - 1].1);
    for _ in 0..d.pick(3) {
        let kind = PyValue::str(KINDS[d.pick(4) as usize]);
        let ts = PyValue::str(&draw_dt(d, false).isoformat());
        let note = if d.pick(2) != 0 {
            PyValue::None
        } else {
            PyValue::Str(pick_text(d, t))
        };
        m.resolution_timeline.push(PyValue::Dict(vec![
            (PyValue::str("kind"), kind),
            (PyValue::str("ts"), ts),
            (PyValue::str("note"), note),
        ]));
    }
    m
}

fn draw_marker(d: &mut Draw, t: &[PyStr]) -> hm::MarkerStats {
    let marker = pick_text(d, t);
    let fire_count = draw_count(d);
    let override_count = draw_count(d);
    hm::MarkerStats {
        marker,
        fire_count,
        override_count,
    }
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

fn stats_sweep(seed: u64, count: usize) -> String {
    let t = texts();
    let mut d = Draw(seed);
    let mut lines = Vec::with_capacity(count);
    for i in 0..count {
        let dict = if i % 4 == 3 {
            Ok(draw_marker(&mut d, &t).to_dict())
        } else {
            draw_memory(&mut d, i, &t).to_dict()
        };
        lines.push(match dict {
            Ok(v) => ascii(&json::dumps(&v, DumpOptions::DEFAULT).expect("dumps")),
            Err(e) => e.name().to_owned(),
        });
    }
    digest(&lines)
}

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

fn round_sweep(seed: u64, count: usize) -> String {
    let mut d = Draw(seed);
    let mut lines = Vec::with_capacity(count * 2);
    for _ in 0..count {
        let x = draw_float(&mut d);
        for n in [3, 4] {
            let r = round_digits(x, n);
            lines.push(format!(
                "{:016x} {n} {:016x} {}",
                x.to_bits(),
                r.to_bits(),
                float_repr::repr(r)
            ));
        }
    }
    digest(&lines)
}

fn ts_sweep(seed: u64, count: usize) -> String {
    let mut d = Draw(seed);
    let mut lines = Vec::with_capacity(count);
    for _ in 0..count {
        let created = draw_dt(&mut d, false);
        let updated = draw_dt(&mut d, false);
        let verified = draw_opt_dt(&mut d, false);
        let corroborated = draw_opt_dt(&mut d, false);
        let ts =
            hm::freshest_touch_ts(&created, &updated, verified.as_ref(), corroborated.as_ref())
                .expect("aware");
        lines.push(format!("{:016x}", ts.to_bits()));
    }
    digest(&lines)
}

#[test]
fn health_model_every_row() {
    let lines = jsonl(NAME);
    let header = &lines[0];
    let count = header["count"].as_u64().expect("count") as usize;
    assert_eq!(
        lines.len(),
        count + 4,
        "{NAME}: header count and three sweeps"
    );
    assert_eq!(header["grace_days"], json!(hm::ENDORSEMENT_GRACE_DAYS));
    assert_eq!(
        text_field(header, "hookless_reason"),
        unicode::cps(hm::HOOKLESS_REASON)
    );
    let mut failures = Vec::new();
    let mut checked = 0;
    for (i, row) in lines[1..=count].iter().enumerate() {
        let ok = match row["k"].as_str().expect("kind") {
            "round" => check_round(row),
            "ts" => check_ts(row),
            "unres" => check_unres(row),
            "ev" => check_ev(row),
            "dead" => check_dead(row),
            "ratio" => check_ratio(row),
            "memory" => check_memory(row),
            "marker" => check_marker(row),
            "scope" => check_scope(row),
            "actor" => check_actor(row),
            "coverage" => check_coverage(row),
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
fn health_model_sweeps() {
    let lines = jsonl(NAME);
    let count = lines[0]["count"].as_u64().expect("count") as usize;
    let sweeps = &lines[count + 1..];
    let kinds: Vec<&str> = sweeps.iter().map(|s| s["k"].as_str().expect("k")).collect();
    assert_eq!(kinds, ["stats", "round", "ts"]);
    for s in sweeps {
        let seed = s["seed"].as_u64().expect("seed");
        let n = s["count"].as_u64().expect("count") as usize;
        assert!(n > 0, "an empty sweep");
        let got = match s["k"].as_str().expect("k") {
            "stats" => stats_sweep(seed, n),
            "round" => round_sweep(seed, n),
            _ => ts_sweep(seed, n),
        };
        assert_eq!(got, s["sha256"].as_str().expect("sha256"), "sweep {s}");
    }
}
