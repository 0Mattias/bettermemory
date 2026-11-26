//! Every row of `eval_render.jsonl` and `eval_render_unicode.jsonl`
//! against `eval_render` and `eval::EvalReport`: `render_text` and
//! `to_dict` of each report spec, `_review_text_lines` and
//! `_surface_text_lines` of each dict spec, `_format_rate` of a `RateCI`
//! and of `_rate_from_dict`, `_bar`, `'0.2f'` and `'g'`
//! (`float_repr::format_fixed`, `format_general`), `_humanize_seconds`,
//! `_short_ts` and `_truncate`; and the sweep of drawn reports by count
//! and digest, drawn here by the generator's rule.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval::EvalReport;
use bettermemory_core::eval_model::{ColdEndorsementMemoriesRow, RateCI, SilentMissCandidate};
use bettermemory_core::eval_render as er;
use bettermemory_core::float_repr;
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const NAME: &str = "eval_render.jsonl";
const UNICODE: &str = "eval_render_unicode.jsonl";

/// `json.dumps(value, indent=1)`.
const INDENT1: DumpOptions = DumpOptions {
    sort_keys: false,
    ensure_ascii: true,
    item_separator: ",",
    key_separator: ": ",
    indent: Some(1),
    default_str: false,
};

/// A text as the generator's `_text` writes it.
fn text(v: &Value) -> PyStr {
    match v {
        Value::String(s) => unicode::cps(s),
        other => common::cps_of(&other["cps"]),
    }
}

fn text_json(s: &[u32]) -> Value {
    match unicode::to_string(s) {
        Some(t) => json!(t),
        None => json!({ "cps": s }),
    }
}

/// A float as its `repr`, checked to read back to the same text.
fn float(v: &Value) -> f64 {
    let spelled = v.as_str().expect("a float's repr");
    let f: f64 = spelled.parse().expect("a float");
    assert_eq!(float_repr::repr(f), spelled, "repr round trip");
    f
}

fn opt_float(v: &Value) -> Option<f64> {
    (!v.is_null()).then(|| float(v))
}

fn int(v: &Value) -> i64 {
    v.as_i64().expect("an int")
}

/// The generator's `pv` spelling of a Python value.
fn pv(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) => PyValue::int(i128::from(n.as_i64().expect("a 64-bit int"))),
        Value::String(s) => PyValue::str(s),
        Value::Array(items) => PyValue::List(items.iter().map(pv).collect()),
        Value::Object(m) => {
            if let Some(f) = m.get("f") {
                PyValue::Float(float(f))
            } else if let Some(digits) = m.get("int") {
                let zeros = m["zeros"].as_u64().expect("zeros") as usize;
                let text = format!("{}{}", digits.as_str().expect("digits"), "0".repeat(zeros));
                PyValue::int_from_decimal(&text).expect("an int")
            } else if m.contains_key("cps") {
                PyValue::Str(text(v))
            } else if let Some(items) = m.get("tuple") {
                PyValue::Tuple(items.as_array().expect("items").iter().map(pv).collect())
            } else if let Some(pairs) = m.get("d") {
                PyValue::Dict(
                    pairs
                        .as_array()
                        .expect("pairs")
                        .iter()
                        .map(|p| (pv(&p[0]), pv(&p[1])))
                        .collect(),
                )
            } else if let Some(iso) = m.get("dt") {
                let iso = iso.as_str().expect("iso");
                let dt = DateTime::from_isoformat(&unicode::cps(iso)).expect("an isoformat");
                assert_eq!(dt.isoformat(), iso);
                PyValue::DateTime(dt)
            } else if let Some(p) = m.get("path") {
                PyValue::Path(unicode::cps(p.as_str().expect("a path")))
            } else {
                panic!("no value spelled {v}")
            }
        }
    }
}

/// The base dict with each edit applied: a key along the path set (in
/// place when it exists, last when new) or removed.
fn edited(base: &PyValue, with: &Value) -> PyValue {
    let mut out = base.clone();
    for edit in with.as_array().expect("edits") {
        let path: Vec<&str> = edit[0]
            .as_array()
            .expect("a path")
            .iter()
            .map(|k| k.as_str().expect("a key"))
            .collect();
        let mut at = &mut out;
        for key in &path[..path.len() - 1] {
            let PyValue::Dict(pairs) = at else {
                panic!("a dict")
            };
            let key = PyValue::str(key);
            at = &mut pairs.iter_mut().find(|(k, _)| *k == key).expect("a key").1;
        }
        let PyValue::Dict(pairs) = at else {
            panic!("a dict")
        };
        let key = PyValue::str(path[path.len() - 1]);
        let found = pairs.iter().position(|(k, _)| *k == key);
        match (edit.get(1), found) {
            (Some(v), Some(i)) => pairs[i].1 = pv(v),
            (Some(v), None) => pairs.push((key, pv(v))),
            (None, Some(i)) => {
                pairs.remove(i);
            }
            (None, None) => panic!("no key to remove"),
        }
    }
    out
}

fn dict_spec(spec: &Value, header: &Value) -> Option<PyValue> {
    if spec.is_null() {
        return None;
    }
    if let Some(v) = spec.get("v") {
        return Some(pv(v));
    }
    let base = spec["base"].as_str().expect("a base");
    Some(edited(&pv(&header[base]), &spec["with"]))
}

fn dt(v: &Value) -> Option<DateTime> {
    v.as_str()
        .map(|iso| DateTime::from_isoformat(&unicode::cps(iso)).expect("an isoformat"))
}

/// The report a spec names.
fn build(spec: &Value, header: &Value) -> EvalReport {
    let gen = spec.get("gen").unwrap_or(&header["generated_at"]);
    let mut r = EvalReport::new(dt(gen).expect("generated_at"));
    if let Some(ws) = spec.get("ws") {
        r.window_seconds = ws.as_i64();
    }
    if let Some(scope) = spec.get("scope") {
        r.scope_filter = (!scope.is_null()).then(|| text(scope));
    }
    if let Some(rule) = spec.get("rule") {
        r.threshold_rule = text(rule);
    }
    for (name, n) in spec
        .get("n")
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
    {
        let n = int(n);
        match name.as_str() {
            "total_events_scanned" => r.total_events_scanned = n,
            "events_in_window" => r.events_in_window = n,
            "retrieval_occurrences" => r.retrieval_occurrences = n,
            "explicit_endorsements_with_excerpt" => r.explicit_endorsements_with_excerpt = n,
            "applied_total" => r.applied_total = n,
            "applied_explicit" => r.applied_explicit = n,
            "turns_audited" => r.turns_audited = n,
            "turns_no_signal" => r.turns_no_signal = n,
            "silent_misses" => r.silent_misses = n,
            "cold_endorsement_memories_total" => r.cold_endorsement_memories_total = n,
            "endorsement_min_retrievals" => r.endorsement_min_retrievals = n,
            "repeat_audits" => r.repeat_audits = n,
            "applied_model" => r.applied_model = n,
            "applied_hook" => r.applied_hook = n,
            other => panic!("no int field {other}"),
        }
    }
    for (name, f) in spec
        .get("rates")
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
    {
        let rate = RateCI {
            numerator: int(&f[0]),
            denominator: int(&f[1]),
            rate: opt_float(&f[2]),
            lower: opt_float(&f[3]),
            upper: opt_float(&f[4]),
            torn_read: f[5].as_bool().expect("torn_read"),
        };
        match name.as_str() {
            "memory_helped_rate" => r.memory_helped_rate = rate,
            "endorsement_rate" => r.endorsement_rate = rate,
            "silent_miss_rate" => r.silent_miss_rate = rate,
            other => panic!("no rate {other}"),
        }
    }
    if let Some(ratio) = spec.get("ratio") {
        r.cold_endorsement_ratio_threshold = float(ratio);
    }
    if let Some(s) = spec.get("suppressed") {
        r.cold_endorsement_suppressed = s.as_bool().expect("a bool");
    }
    for row in spec
        .get("cold")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        r.cold_endorsement_memories_rows
            .push(ColdEndorsementMemoriesRow {
                id: text(&row[0]),
                scopes: row[1]
                    .as_array()
                    .expect("scopes")
                    .iter()
                    .map(text)
                    .collect(),
                summary: text(&row[2]),
                retrieval_count: int(&row[3]),
                auto_applied_count: int(&row[4]),
                explicit_applied_count: int(&row[5]),
            });
    }
    let opt_text = |v: &Value| (!v.is_null()).then(|| text(v));
    for m in spec
        .get("misses")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        r.silent_miss_recent.push(SilentMissCandidate {
            ts: text(&m[0]),
            session_id: text(&m[1]),
            top_missed_id: opt_text(&m[2]),
            top_missed_relevance: opt_text(&m[3]),
            threshold_rule: opt_text(&m[4]),
            recent_retrieval_count: int(&m[5]).to_string(),
        });
    }
    for m in spec
        .get("by_model")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let counts = m[1]
            .as_array()
            .expect("counts")
            .iter()
            .map(|kv| (text(&kv[0]), int(&kv[1])))
            .collect();
        r.by_model.push((text(&m[0]), counts));
    }
    if let Some(start) = spec.get("start") {
        r.window_start = dt(start);
    }
    if let Some(end) = spec.get("end") {
        r.window_end = dt(end);
    }
    if let Some(review) = spec.get("review") {
        r.review = dict_spec(review, header);
    }
    if let Some(surfaces) = spec.get("surfaces") {
        r.surfaces = dict_spec(surfaces, header);
    }
    r
}

/// `text.split("\n")`.
fn split_lines(text: &[u32]) -> Vec<PyStr> {
    text.split(|&c| c == u32::from(b'\n'))
        .map(<[u32]>::to_vec)
        .collect()
}

/// The generator's `ops` applied to `base`.
fn apply(base: &[PyStr], ops: &Value) -> Vec<PyStr> {
    let mut out = Vec::new();
    let mut at = 0;
    for op in ops.as_array().expect("ops") {
        let keep = op[0].as_u64().expect("keep") as usize;
        let drop = op[1].as_u64().expect("drop") as usize;
        out.extend_from_slice(&base[at..at + keep]);
        at += keep + drop;
        out.extend(op[2].as_array().expect("lines").iter().map(text));
    }
    out.extend_from_slice(&base[at..]);
    out
}

fn lines_json(lines: &[PyStr]) -> Value {
    Value::Array(lines.iter().map(|l| text_json(l)).collect())
}

fn base_lines(header: &Value, key: &str) -> Vec<PyStr> {
    header[key]
        .as_array()
        .expect("lines")
        .iter()
        .map(text)
        .collect()
}

/// Lines or an exception class, as JSON.
fn lines_answer(got: Result<Vec<PyStr>, er::RenderError>) -> Value {
    match got {
        Ok(lines) => lines_json(&lines),
        Err(e) => json!({ "x": e.name() }),
    }
}

fn want_lines(row: &Value, base: &[PyStr]) -> Value {
    match row.get("x") {
        Some(x) => json!({ "x": x }),
        None => lines_json(&apply(base, &row["ops"])),
    }
}

fn text_answer(got: Result<PyStr, er::RenderError>) -> Value {
    match got {
        Ok(t) => text_json(&t),
        Err(e) => json!({ "x": e.name() }),
    }
}

fn want_text(row: &Value) -> Value {
    match row.get("x") {
        Some(x) => json!({ "x": x }),
        None => text_json(&common::text_field(row, "r")),
    }
}

fn dump(value: &PyValue, options: DumpOptions) -> PyStr {
    json::dumps(value, options).expect("to_dict dumps")
}

fn sha256(text: &[u32]) -> String {
    let utf8 = unicode::to_string(text).expect("UTF-8");
    common::hex(&Sha256::digest(utf8.as_bytes()))
}

fn check(row: &Value, header: &Value) -> (Value, Value) {
    match row["k"].as_str().expect("a kind") {
        "report" => {
            let report = build(&row["in"], header);
            let got_text = lines_answer(er::render_text(&report).map(|t| split_lines(&t)));
            let want_text = want_lines(row, &base_lines(header, "report_lines"));
            let dumped = dump(&report.to_dict(), INDENT1);
            let (got_dict, want_dict) = match row.get("dict") {
                Some(ops) => (
                    lines_json(&split_lines(&dumped)),
                    lines_json(&apply(&base_lines(header, "dict_lines"), ops)),
                ),
                None => (json!(sha256(&dumped)), row["dict_sha256"].clone()),
            };
            (json!([got_text, got_dict]), json!([want_text, want_dict]))
        }
        kind @ ("review" | "surfaces") => {
            let value = dict_spec(&row["in"], header);
            let got = if kind == "review" {
                er::review_text_lines(value.as_ref())
            } else {
                er::surface_text_lines(value.as_ref())
            };
            let base = base_lines(header, &format!("{kind}_lines"));
            (lines_answer(got), want_lines(row, &base))
        }
        "rate" => {
            let f = &row["rate"];
            let rate = RateCI {
                numerator: int(&f[0]),
                denominator: int(&f[1]),
                rate: opt_float(&f[2]),
                lower: opt_float(&f[3]),
                upper: opt_float(&f[4]),
                torn_read: f[5].as_bool().expect("torn_read"),
            };
            let got = er::format_rate(&text(&row["label"]), &rate);
            (text_json(&got), want_text(row))
        }
        "rate_dict" => {
            let d = pv(&row["in"]);
            let label = text(&row["label"]);
            let got = er::rate_from_dict(&d).and_then(|f| er::format_rate_fields(&label, &f));
            (text_answer(got), want_text(row))
        }
        "bar" => (text_json(&er::bar(float(&row["v"]))), want_text(row)),
        "fixed" => (
            json!(float_repr::format_fixed(float(&row["v"]), 2).ok()),
            row["r"].clone(),
        ),
        "general" => (
            json!(float_repr::format_general(float(&row["v"]))),
            row["r"].clone(),
        ),
        "humanize" => (
            json!(er::humanize_seconds(int(&row["v"]))),
            row["r"].clone(),
        ),
        "short_ts" => (text_json(&er::short_ts(&text(&row["v"]))), want_text(row)),
        "truncate" => {
            let n = row["n"].as_u64().expect("n") as usize;
            (
                text_json(&er::truncate(&text(&row["v"]), n)),
                want_text(row),
            )
        }
        other => panic!("unknown kind {other}"),
    }
}

fn every_row(test: &str, name: &str, trailer: usize) {
    let lines = common::jsonl(name);
    let rows = &lines[1..lines.len() - trailer];
    assert_eq!(
        lines[0]["count"].as_u64().expect("count") as usize,
        rows.len()
    );
    let full = common::jsonl(NAME);
    let header = &full[0];
    let mut ledger = Ledger::new(test, name);
    let mut failures = Vec::new();
    for (i, row) in rows.iter().enumerate() {
        let (got, want) = check(row, header);
        if ledger.differs_fatally(format!("row {i}"), got == want) {
            failures.push(format!(
                "row {i} {row}\n native    {got}\n reference {want}"
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
fn eval_render_every_row() {
    every_row("golden_eval_render", NAME, 1);
}

#[test]
fn eval_render_unicode_every_row() {
    every_row("golden_eval_render", UNICODE, 0);
}

#[test]
fn header_constants() {
    let header = &common::jsonl(NAME)[0];
    let rates: Vec<&str> = header["surface_rates"]
        .as_array()
        .expect("rates")
        .iter()
        .map(|r| r.as_str().expect("a name"))
        .collect();
    assert_eq!(rates, er::SURFACE_RATES);
    // The base texts are the renderers' own answers on the base values.
    let empty = EvalReport::new(dt(&header["generated_at"]).expect("generated_at"));
    let text = er::render_text(&empty).expect("the empty report");
    assert_eq!(split_lines(&text), base_lines(header, "report_lines"));
    let dumped = dump(&empty.to_dict(), INDENT1);
    assert_eq!(split_lines(&dumped), base_lines(header, "dict_lines"));
    let review = pv(&header["review"]);
    assert_eq!(
        er::review_text_lines(Some(&review)).expect("the base review"),
        base_lines(header, "review_lines")
    );
    let surfaces = pv(&header["surfaces"]);
    assert_eq!(
        er::surface_text_lines(Some(&surfaces)).expect("the base surfaces"),
        base_lines(header, "surfaces_lines")
    );
}

// ---------------------------------------------------------------------------
// the sweep, drawn by the generator's rule
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

    fn pick(&mut self, n: usize) -> usize {
        (self.nxt() % n as u64) as usize
    }
}

const WORDS: [&str; 8] = ["a", "repo:x", "global", "B", "zeta", "mem", "v1", "x y"];
const MODELS: [&str; 7] = ["claude", "Claude", "gpt", "_", "a", "aa", "z9"];
const COUNTS: [i64; 9] = [0, 1, 7, 99999, 100000, 123456, -3, i64::MAX, i64::MIN];
const SECONDS: [i64; 12] = [
    0, 1, 59, 60, 90, 3600, 5400, 86400, 172800, -60, -86400, 2592000,
];
const DATES: [&str; 3] = [
    "2026-10-01T00:00:00+00:00",
    "2026-11-25T12:30:45.123456+00:00",
    "2026-11-25T12:00:00+02:00",
];
const STAMPS: [&str; 4] = [
    "2026-11-25T12:34:56Z",
    "2026-11-25",
    "xxxxxxxxxxxxxxxxxxxx",
    "2026-11-25T1",
];
const COUNT_KEYS: [&str; 3] = ["audited", "misses", "no_signal"];

fn ratios() -> [f64; 9] {
    [
        0.0,
        0.0,
        0.25,
        1e-05,
        0.1 + 0.2,
        1234567.0,
        f64::NAN,
        -0.5,
        f64::INFINITY,
    ]
}

fn draw_count(d: &mut Draw) -> i64 {
    if d.pick(2) == 0 {
        COUNTS[d.pick(COUNTS.len())]
    } else {
        d.pick(1000) as i64
    }
}

fn draw_text(d: &mut Draw) -> Option<PyStr> {
    match d.pick(3) {
        0 => None,
        1 => Some(Vec::new()),
        _ => Some(unicode::cps(WORDS[d.pick(WORDS.len())])),
    }
}

fn iso(s: &str) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(s)).expect("an isoformat")
}

/// The generator's `draw_report`, step for step.
fn draw_report(d: &mut Draw, header: &Value) -> EvalReport {
    let mut r = EvalReport::new(iso(header["generated_at"].as_str().expect("iso")));
    r.total_events_scanned = d.pick(1000) as i64;
    r.events_in_window = draw_count(d);
    r.retrieval_occurrences = draw_count(d);
    r.explicit_endorsements_with_excerpt = d.pick(1000) as i64;
    r.applied_total = draw_count(d);
    r.applied_explicit = d.pick(1000) as i64;
    r.turns_audited = draw_count(d);
    r.turns_no_signal = draw_count(d);
    r.silent_misses = d.pick(1000) as i64;
    let w = d.pick(3);
    if w >= 1 {
        r.window_seconds = Some(SECONDS[d.pick(SECONDS.len())]);
    }
    if w == 2 {
        r.window_end = Some(iso(DATES[d.pick(DATES.len())]));
        if d.pick(2) == 1 {
            r.window_start = Some(iso(DATES[d.pick(DATES.len())]));
        }
    }
    r.scope_filter = draw_text(d);
    r.threshold_rule = draw_text(d).unwrap_or_default();
    r.repeat_audits = draw_count(d);
    r.applied_model = d.pick(1000) as i64;
    r.applied_hook = d.pick(1000) as i64;
    for i in 0..3 {
        if d.pick(4) != 0 {
            let k = d.pick(12) as i64;
            let n = d.pick(12) as i64;
            let rate = RateCI::from_counts(k, n);
            match i {
                0 => r.memory_helped_rate = rate,
                1 => r.endorsement_rate = rate,
                _ => r.silent_miss_rate = rate,
            }
        }
    }
    let summary = unicode::cps(&"abc ".repeat(20));
    for _ in 0..d.pick(13) {
        let id = unicode::cps(&format!("m{}", d.pick(100000)));
        let scopes = (0..d.pick(3))
            .map(|_| unicode::cps(WORDS[d.pick(WORDS.len())]))
            .collect();
        let cut = d.pick(60);
        r.cold_endorsement_memories_rows
            .push(ColdEndorsementMemoriesRow {
                id,
                scopes,
                summary: summary[..cut].to_vec(),
                retrieval_count: d.pick(500) as i64,
                auto_applied_count: d.pick(50) as i64,
                explicit_applied_count: d.pick(50) as i64,
            });
    }
    let rows = r.cold_endorsement_memories_rows.len() as i64;
    r.cold_endorsement_memories_total = match d.pick(3) {
        0 => rows,
        1 => 0,
        _ => rows + d.pick(30) as i64,
    };
    if d.pick(2) == 1 {
        r.endorsement_min_retrievals = d.pick(100) as i64;
    }
    r.cold_endorsement_ratio_threshold = ratios()[d.pick(9)];
    r.cold_endorsement_suppressed = d.pick(2) == 1;
    for _ in 0..d.pick(4) {
        let ts = unicode::cps(STAMPS[d.pick(STAMPS.len())]);
        let session_id = unicode::cps(&format!("sess-{}", d.pick(1_000_000_000)));
        let top_missed_id = draw_text(d);
        let top_missed_relevance = draw_text(d);
        let threshold_rule = draw_text(d);
        r.silent_miss_recent.push(SilentMissCandidate {
            ts,
            session_id,
            top_missed_id,
            top_missed_relevance,
            threshold_rule,
            recent_retrieval_count: d.pick(10).to_string(),
        });
    }
    let start = d.pick(MODELS.len());
    for i in 0..d.pick(4) {
        let mut counts = Vec::new();
        for key in COUNT_KEYS {
            if d.pick(3) != 0 {
                counts.push((unicode::cps(key), d.pick(1000) as i64));
            }
        }
        r.by_model
            .push((unicode::cps(MODELS[(start + i) % MODELS.len()]), counts));
    }
    if d.pick(4) == 0 {
        r.review = Some(pv(&header["review"]));
    }
    if d.pick(4) == 0 {
        r.surfaces = Some(pv(&header["surfaces"]));
    }
    r
}

#[test]
fn eval_render_sweep() {
    let lines = common::jsonl(NAME);
    let header = &lines[0];
    let sweep = lines.last().expect("the sweep");
    assert_eq!(sweep["k"], "sweep");
    let count = sweep["count"].as_u64().expect("count") as usize;
    let mut d = Draw(sweep["seed"].as_u64().expect("seed"));
    let mut digest = Sha256::new();
    let mut drawn = 0;
    for _ in 0..count {
        let report = draw_report(&mut d, header);
        let text = er::render_text(&report).expect("a drawn report renders");
        digest.update(unicode::to_string(&text).expect("UTF-8").as_bytes());
        let dumped = dump(&report.to_dict(), DumpOptions::DEFAULT);
        digest.update(unicode::to_string(&dumped).expect("ASCII").as_bytes());
        drawn += 1;
    }
    assert!(count > 0);
    assert_eq!(drawn, count);
    let got = common::hex(&digest.finalize());
    assert_eq!(
        got,
        sweep["sha256"].as_str().expect("sha256"),
        "the sweep differs"
    );
}
