//! Every row of `eval_rates.jsonl` and `eval_rates_unicode.jsonl` against
//! `eval_rates`: `walk_eval_events` (with `count_search` and `count_use`)
//! over each log, the `passes_scope` calls in order, and
//! `resolve_audit_telemetry` and `cold_endorsement_rows` of the walk under
//! each set of arguments; and the sweep of drawn logs by count and digest,
//! drawn here by the generator's rule.

mod common;

use std::collections::{HashMap, HashSet};

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval_rates::{
    cold_endorsement_rows, resolve_audit_telemetry, walk_eval_events, AuditTally, ColdRows, Counts,
    EvalWalk,
};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::{Category, Confidence, Memory, RecordFields, Source};
use bettermemory_core::unicode::{self, cps};
use common::parity::Ledger;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

const NAME: &str = "eval_rates.jsonl";
const UNICODE: &str = "eval_rates_unicode.jsonl";

/// A JSON value as `json.loads` gives it to the reference.
fn plain(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) => match n.as_i64() {
            Some(i) => PyValue::int(i128::from(i)),
            None => PyValue::Float(n.as_f64().expect("a float")),
        },
        Value::String(s) => PyValue::str(s),
        Value::Array(items) => PyValue::List(items.iter().map(plain).collect()),
        Value::Object(m) => {
            PyValue::Dict(m.iter().map(|(k, v)| (PyValue::str(k), plain(v))).collect())
        }
    }
}

fn text(s: &[u32]) -> Value {
    json!(unicode::to_string(s).expect("no surrogate"))
}

fn opt_text(s: Option<&[u32]>) -> Value {
    s.map_or(Value::Null, text)
}

fn dt(text: &str) -> DateTime {
    DateTime::from_isoformat(&cps(text)).expect("an isoformat")
}

fn memory(id: &str, scopes: &[&str], body: &str, category: Option<&str>) -> Memory {
    let at = dt("2026-10-01T00:00:00+00:00");
    Memory {
        fields: RecordFields {
            id: cps(id),
            created: at,
            updated: at,
            scopes: scopes.iter().map(|s| cps(s)).collect(),
            confidence: Confidence::MEMBERS[0].1,
            source: Source::MEMBERS[0].1,
            body: cps(body),
            origin: None,
            actor: None,
            last_verified_at: None,
            category: category.map(|c| match c {
                "fact" => Category::Fact,
                "user-inference" => Category::UserInference,
                "ambient" => Category::Ambient,
                other => panic!("no category {other}"),
            }),
            verified_paths: Vec::new(),
            verified_commits: Vec::new(),
            verified_versions: Vec::new(),
            verified_absent_paths: Vec::new(),
            verified_path_targets: None,
            verified_absent_path_targets: None,
            claims: Vec::new(),
            verified_head: None,
        },
        links: Vec::new(),
        corroborations: "0".to_owned(),
        last_corroborated: None,
    }
}

/// The header's `[key, id, scopes, body, category]` rows as `by_id`.
fn memories(table: &Value) -> HashMap<PyStr, Memory> {
    table
        .as_array()
        .expect("memories")
        .iter()
        .map(|m| {
            let scopes: Vec<&str> = m[2]
                .as_array()
                .expect("scopes")
                .iter()
                .map(|s| s.as_str().expect("a scope"))
                .collect();
            (
                cps(m[0].as_str().expect("a key")),
                memory(
                    m[1].as_str().expect("an id"),
                    &scopes,
                    m[3].as_str().expect("a body"),
                    m[4].as_str(),
                ),
            )
        })
        .collect()
}

fn counts(c: &Counts) -> Value {
    Value::Array(c.items().iter().map(|(k, n)| json!([text(k), n])).collect())
}

fn positions(rows: &[&PyValue], events: &[PyValue]) -> Value {
    Value::Array(
        rows.iter()
            .map(|r| {
                json!(events
                    .iter()
                    .position(|e| std::ptr::eq(e, *r))
                    .expect("a row of the log"))
            })
            .collect(),
    )
}

fn walk_json(w: &EvalWalk<'_>, events: &[PyValue]) -> Value {
    let mut acked: Vec<&PyStr> = w.acknowledged_miss_event_ids.iter().collect();
    acked.sort();
    json!({
        "review": positions(&w.review_rows, events),
        "surface": positions(&w.surface_rows, events),
        "audit": positions(&w.audit_telemetry, events),
        "n": [
            w.retrieval_occurrences,
            w.explicit_endorsements_with_excerpt,
            w.applied_total,
            w.applied_explicit,
            w.applied_model,
            w.applied_hook,
            w.total_events_scanned,
            w.events_in_window,
            w.hook_telemetry_events,
        ],
        "search": counts(&w.search_delivery_count),
        "auto": counts(&w.auto_applied_count),
        "explicit": counts(&w.explicit_applied_count),
        "cutoff": w.latest_miss_cutoff.as_ref().map(DateTime::isoformat),
        "acked": acked.into_iter().map(|a| text(a)).collect::<Vec<_>>(),
    })
}

fn tally_json(t: &AuditTally) -> Value {
    json!({
        "n": [t.turns_audited, t.turns_no_signal, t.silent_misses, t.repeat_audits],
        "rule": text(&t.threshold_rule),
        "models": t.by_model.iter().map(|(m, c)| {
            json!([text(m), c.audited, c.no_signal, c.misses])
        }).collect::<Vec<_>>(),
        "buffer": t.silent_miss_buffer.iter().map(|c| json!([
            text(&c.ts),
            text(&c.session_id),
            opt_text(c.top_missed_id.as_deref()),
            opt_text(c.top_missed_relevance.as_deref()),
            opt_text(c.threshold_rule.as_deref()),
            c.recent_retrieval_count,
        ])).collect::<Vec<_>>(),
    })
}

fn cold_json(c: &ColdRows) -> Value {
    json!({
        "rows": c.rows.iter().map(|r| json!([
            text(&r.id),
            r.scopes.iter().map(|s| text(s)).collect::<Vec<_>>(),
            text(&r.summary),
            r.retrieval_count,
            r.auto_applied_count,
            r.explicit_applied_count,
        ])).collect::<Vec<_>>(),
        "total": c.total,
        "suppressed": c.suppressed,
    })
}

/// One log's arguments, as a row or a draw of the sweep holds them.
struct Log {
    events: Vec<PyValue>,
    cutoff: Option<String>,
    end: Option<String>,
    reject: HashSet<PyStr>,
    resolves: Vec<(Vec<String>, i64)>,
    colds: Vec<(i64, String)>,
}

/// The generator's `answer`: the calls, the walk or the exception's
/// class, and each resolve and cold list.
fn answer(log: &Log, by_id: &HashMap<PyStr, Memory>) -> Value {
    let mut calls: Vec<Value> = Vec::new();
    let cutoff = log.cutoff.as_deref().map(dt);
    let end = log.end.as_deref().map(dt);
    let walked = walk_eval_events(&log.events, cutoff.as_ref(), end.as_ref(), &mut |mid| {
        let mid = mid.expect("every call passes a str");
        calls.push(text(mid));
        !log.reject.contains(mid)
    });
    let mut out = Map::new();
    let walk = match walked {
        Ok(walk) => walk,
        Err(e) => {
            out.insert("calls".into(), Value::Array(calls));
            out.insert("x".into(), json!(e.name()));
            return Value::Object(out);
        }
    };
    out.insert("walk".into(), walk_json(&walk, &log.events));
    let resolve: Vec<Value> = log
        .resolves
        .iter()
        .map(|(tombstoned, limit)| {
            let set: HashSet<PyStr> = tombstoned.iter().map(|t| cps(t)).collect();
            let tally = match resolve_audit_telemetry(&walk, &set, *limit) {
                Ok(t) => tally_json(&t),
                Err(e) => json!({ "x": e.name() }),
            };
            json!([tombstoned, limit, tally])
        })
        .collect();
    let cold: Vec<Value> = log
        .colds
        .iter()
        .map(|(floor, ratio)| {
            let r: f64 = ratio.parse().expect("a float's repr");
            json!([
                floor,
                ratio,
                cold_json(&cold_endorsement_rows(&walk, by_id, *floor, r))
            ])
        })
        .collect();
    out.insert("calls".into(), Value::Array(calls));
    out.insert("resolve".into(), Value::Array(resolve));
    out.insert("cold".into(), Value::Array(cold));
    Value::Object(out)
}

fn opt_string(v: &Value) -> Option<String> {
    v.as_str().map(str::to_owned)
}

fn row_log(row: &Value) -> Log {
    let pairs = |key: &str| -> Vec<Value> {
        row.get(key)
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
    };
    Log {
        events: row["events"]
            .as_array()
            .expect("events")
            .iter()
            .map(plain)
            .collect(),
        cutoff: opt_string(&row["cutoff"]),
        end: opt_string(&row["end"]),
        reject: row["reject"]
            .as_array()
            .expect("reject")
            .iter()
            .map(|r| cps(r.as_str().expect("an id")))
            .collect(),
        resolves: pairs("resolve")
            .iter()
            .map(|r| {
                let t = r[0]
                    .as_array()
                    .expect("tombstones")
                    .iter()
                    .map(|s| s.as_str().expect("an id").to_owned())
                    .collect();
                (t, r[1].as_i64().expect("a limit"))
            })
            .collect(),
        colds: pairs("cold")
            .iter()
            .map(|c| {
                (
                    c[0].as_i64().expect("a floor"),
                    c[1].as_str().expect("a ratio").to_owned(),
                )
            })
            .collect(),
    }
}

/// The answer part of a row: everything but its inputs.
fn expected(row: &Value) -> Value {
    let mut out = Map::new();
    for key in ["calls", "x", "walk", "resolve", "cold"] {
        if let Some(v) = row.get(key) {
            out.insert(key.into(), v.clone());
        }
    }
    Value::Object(out)
}

fn every_row(test: &str, name: &str, trailer: usize) {
    let lines = common::jsonl(name);
    let rows = &lines[1..lines.len() - trailer];
    assert_eq!(
        lines[0]["count"].as_u64().expect("count") as usize,
        rows.len()
    );
    let by_id = memories(&lines[0]["memories"]);
    let mut ledger = Ledger::new(test, name);
    let mut failures = Vec::new();
    for (i, row) in rows.iter().enumerate() {
        assert_eq!(row["k"], "log");
        let got = answer(&row_log(row), &by_id);
        let want = expected(row);
        if ledger.differs_fatally(format!("row {i}"), got == want) {
            failures.push(format!(
                "row {i} {}\n native    {got}\n reference {want}",
                row["name"]
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{name}: {} rows differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
    ledger.finish(rows.len());
}

#[test]
fn eval_rates_every_row() {
    every_row("golden_eval_rates", NAME, 1);
}

#[test]
fn eval_rates_unicode_every_row() {
    every_row("golden_eval_rates", UNICODE, 0);
}

// ---------------------------------------------------------------------------
// the sweep, drawn by the generator's rule
// ---------------------------------------------------------------------------

struct Draw(u64);

impl Draw {
    fn nxt(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        self.0 >> 33
    }

    fn pick(&mut self, n: usize) -> usize {
        (self.nxt() % n as u64) as usize
    }
}

const S_KINDS: [&str; 13] = [
    "search",
    "list",
    "show",
    "use",
    "use",
    "turn_audited",
    "search_miss",
    "silent_miss_cutoff",
    "miss_ack",
    "review_issued",
    "write",
    "session_brief",
    "other",
];
const S_IDS: [&str; 5] = ["a", "b", "c", "d", "e"];
const S_STAMPS: [&str; 6] = [
    "2026-10-01T00:00:00Z",
    "2026-10-04T12:00:00+00:00",
    "2026-10-06T01:00:00+02:00",
    "2026-10-08T00:00:00",
    "2026-10-12T00:00:00.500000Z",
    "bad",
];
const S_BOUNDS: [Option<&str>; 5] = [
    None,
    None,
    Some("2026-10-03T00:00:00+00:00"),
    Some("2026-10-06T00:00:00+00:00"),
    Some("2026-10-09T00:00:00+00:00"),
];
const S_MODELS: [&str; 3] = ["m1", "m2", ""];
const S_RULES: [&str; 3] = ["r1", "r2", ""];
const S_ACKS: [&str; 5] = ["e0", "e1", "e2", "e3", ""];
const S_LIMITS: [i64; 5] = [-1, 0, 1, 2, 20];
const S_RATIOS: [&str; 3] = ["0.0", "0.5", "1.0"];

fn excerpt(i: usize) -> Value {
    [json!(null), json!(""), json!(" "), json!("x"), json!(5)][i].clone()
}

fn draw_ids(d: &mut Draw, n: usize) -> Value {
    let count = d.pick(n);
    Value::Array((0..count).map(|_| json!(S_IDS[d.pick(5)])).collect())
}

/// The generator's `draw_event`. Python evaluates an assignment's value
/// before its subscript, so a drawn value comes before its drawn key.
fn draw_event(d: &mut Draw) -> Value {
    if d.pick(25) == 0 {
        return [json!(null), json!("s"), json!(3), json!([1])][d.pick(4)].clone();
    }
    let kind = S_KINDS[d.pick(S_KINDS.len())];
    let mut ev = Map::new();
    ev.insert("kind".into(), json!(kind));
    let s = d.pick(S_STAMPS.len() + 1);
    if s < S_STAMPS.len() {
        ev.insert("ts".into(), json!(S_STAMPS[s]));
    }
    match kind {
        "search" | "list" => {
            let ids = draw_ids(d, 4);
            let key = ["returned", "memory_ids", "hit_ids"][d.pick(3)];
            ev.insert(key.into(), ids);
            if d.pick(4) == 0 {
                ev.insert("returned".into(), json!([]));
            }
        }
        "show" => {
            ev.insert("id".into(), json!(S_IDS[d.pick(5)]));
        }
        "use" => {
            let outcome = if d.pick(4) != 0 { "applied" } else { "ignored" };
            ev.insert("outcome".into(), json!(outcome));
            match d.pick(3) {
                0 => _ = ev.insert("auto".into(), json!(true)),
                1 => _ = ev.insert("auto".into(), json!(1)),
                _ => {}
            }
            match d.pick(3) {
                0 => _ = ev.insert("attribution".into(), json!("hook")),
                1 => _ = ev.insert("attribution".into(), json!("model")),
                _ => {}
            }
            if d.pick(3) == 0 {
                ev.insert("triggered_from".into(), json!("stop_hook"));
            }
            match d.pick(4) {
                0 => _ = ev.insert("surface".into(), json!("brief")),
                1 => _ = ev.insert("surface".into(), json!("recall")),
                _ => {}
            }
            let ids = draw_ids(d, 5);
            let key = if d.pick(3) != 0 { "ids" } else { "memory_ids" };
            ev.insert(key.into(), ids);
            if d.pick(3) == 0 {
                let count = d.pick(6);
                let list: Vec<Value> = (0..count).map(|_| excerpt(d.pick(5))).collect();
                ev.insert("claim_excerpts".into(), Value::Array(list));
            }
        }
        "turn_audited" | "search_miss" => {
            if kind == "turn_audited" {
                if d.pick(5) == 0 {
                    ev.insert("repeat".into(), json!(true));
                }
                if d.pick(3) == 0 {
                    ev.insert("verdict".into(), json!("no_signal"));
                }
                if d.pick(2) == 0 {
                    ev.insert("triggered_from".into(), json!("stop_hook"));
                }
            } else {
                if d.pick(4) != 0 {
                    ev.insert("session_id".into(), json!("s1"));
                }
                if d.pick(3) != 0 {
                    let id = S_IDS[d.pick(5)];
                    ev.insert("top_hits".into(), json!([{"id": id, "relevance": "high"}]));
                }
                ev.insert("event_id".into(), json!(S_ACKS[d.pick(4)]));
                ev.insert("recent_retrieval_count".into(), json!(d.pick(3)));
            }
            let m = d.pick(4);
            if m < 3 {
                ev.insert("client_model".into(), json!(S_MODELS[m]));
            }
            let r = d.pick(4);
            if r < 3 {
                ev.insert("threshold_rule".into(), json!(S_RULES[r]));
            }
        }
        "silent_miss_cutoff" => {
            ev.insert("cutoff_ts".into(), json!(S_STAMPS[d.pick(S_STAMPS.len())]));
        }
        "miss_ack" => {
            ev.insert("event_id".into(), json!(S_ACKS[d.pick(5)]));
        }
        "review_issued" | "write" => {
            if d.pick(2) != 0 {
                ev.insert("review_id".into(), json!("r"));
            }
        }
        "session_brief" => {
            let ids = draw_ids(d, 3);
            ev.insert("ids".into(), ids);
        }
        _ => {}
    }
    Value::Object(ev)
}

/// The generator's `draw_log`.
fn draw_log(d: &mut Draw) -> Log {
    let count = d.pick(30);
    let events: Vec<PyValue> = (0..count).map(|_| plain(&draw_event(d))).collect();
    let cutoff = S_BOUNDS[d.pick(S_BOUNDS.len())].map(str::to_owned);
    let end = S_BOUNDS[d.pick(S_BOUNDS.len())].map(str::to_owned);
    let reject = S_IDS
        .iter()
        .filter(|_| d.pick(4) == 0)
        .map(|i| cps(i))
        .collect();
    let tombstoned = S_IDS
        .iter()
        .filter(|_| d.pick(3) == 0)
        .map(|i| (*i).to_owned())
        .collect();
    let limit = S_LIMITS[d.pick(5)];
    let floor = d.pick(3) as i64;
    let ratio = S_RATIOS[d.pick(3)].to_owned();
    Log {
        events,
        cutoff,
        end,
        reject,
        resolves: vec![(tombstoned, limit)],
        colds: vec![(floor, ratio)],
    }
}

#[test]
fn eval_rates_sweep() {
    let lines = common::jsonl(NAME);
    let sweep = lines.last().expect("the sweep");
    assert_eq!(sweep["k"], "sweep");
    let count = sweep["count"].as_u64().expect("count") as usize;
    assert_eq!(count, 3000);
    let by_id = memories(&sweep["memories"]);
    let mut d = Draw(sweep["seed"].as_u64().expect("seed"));
    let mut digest = Sha256::new();
    let mut drawn = 0;
    for _ in 0..count {
        let log = draw_log(&mut d);
        let line = serde_json::to_string(&answer(&log, &by_id)).expect("json");
        assert!(line.is_ascii());
        digest.update(line.as_bytes());
        digest.update(b"\n");
        drawn += 1;
    }
    common::assert_count(NAME, drawn, count);
    let got: String = digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(
        got,
        sweep["sha256"].as_str().expect("sha256"),
        "sweep digest"
    );
}
