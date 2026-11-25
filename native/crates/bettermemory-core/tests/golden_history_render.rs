//! Every case of `history_render.jsonl` and `history_render_unicode.jsonl`
//! against `history`'s fifth unit: `actor_text`, `value_text`, and the
//! four renders of each history (`render_text` and `render_json`, with
//! and without the diff), compared as whole texts, code point for code
//! point; the seeded sweep and the code point sweep by count and SHA-256.

mod common;

use bettermemory_core::history_record::RecordHistory;
use bettermemory_core::history_record::ACTOR_FROM_EVENT;
use bettermemory_core::history_render::{
    actor_text, render_json, render_text, value_text, RenderError,
};
use bettermemory_core::history_rows::HistoryRow;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use common::parity::Ledger;
use common::{jsonl, neutral, text_field};
use serde_json::Value;
use sha2::{Digest, Sha256};

fn leak(text: &str) -> &'static str {
    Box::leak(text.to_owned().into_boxed_str())
}

fn opt_text(v: &Value) -> Option<String> {
    v.as_str().map(str::to_owned)
}

fn row_of(r: &Value) -> HistoryRow {
    let changed = match &neutral(&r["ch"]) {
        PyValue::Tuple(items) => items.iter().map(str_of).collect(),
        other => panic!("changed {other:?}"),
    };
    let has_diff = r.get("df").is_some() || r.get("df_cps").is_some();
    HistoryRow {
        seq: r["seq"].as_i64().expect("seq"),
        ts: opt_text(&r["ts"]).expect("ts"),
        session: opt_text(&r["se"]),
        kind: opt_text(&r["k"]).expect("kind"),
        role: leak(r["ro"].as_str().expect("role")),
        action: opt_text(&r["a"]).expect("action"),
        verified: r["v"].as_bool(),
        actor: r.get("ac").map(neutral),
        actor_source: r["as"].as_str().map(leak),
        changed,
        detail: r["d"]
            .as_array()
            .expect("detail")
            .iter()
            .map(|pair| (neutral(&pair[0]), neutral(&pair[1])))
            .collect(),
        diff: has_diff.then(|| text_field(r, "df")),
    }
}

fn history_of(h: &Value) -> RecordHistory {
    RecordHistory {
        record_id: text_field(h, "id"),
        record_kind: h["rk"].as_str().map(leak),
        state: leak(h["st"].as_str().expect("state")),
        provenance: opt_text(&h["pv"]),
        key_present: h["kp"].as_bool().expect("key present"),
        rows: h["rows"]
            .as_array()
            .expect("rows")
            .iter()
            .map(row_of)
            .collect(),
    }
}

fn shown(text: &[u32]) -> String {
    text.iter()
        .map(|&c| char::from_u32(c).unwrap_or('\u{fffd}'))
        .collect()
}

/// The case's answer `name`: the exception's class under `name_e`, else
/// the whole text, compared exactly.
fn expect(case: &Value, name: &str, got: Result<PyStr, &'static str>) {
    match case.get(format!("{name}_e")) {
        Some(e) => assert_eq!(
            got.map(|t| shown(&t)),
            Err(e.as_str().expect("a class name")),
            "{name} of {case}"
        ),
        None => {
            let want = text_field(case, name);
            let got = got.unwrap_or_else(|e| panic!("{name} raised {e} for {case}"));
            assert!(
                got == want,
                "{name} of {case}\n got: {}\nwant: {}",
                shown(&got),
                shown(&want)
            );
        }
    }
}

/// splitmix64, as `gen_history_render.SplitMix` draws.
struct SplitMix(u64);

impl SplitMix {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }

    fn choice<'p, T>(&mut self, pool: &'p [T]) -> &'p T {
        &pool[self.below(pool.len())]
    }
}

fn opt_str(v: &PyValue) -> Option<String> {
    match v {
        PyValue::None => None,
        PyValue::Str(s) => Some(unicode::to_string(s).expect("valid text")),
        other => panic!("a pool text {other:?}"),
    }
}

fn str_of(v: &PyValue) -> PyStr {
    match v {
        PyValue::Str(s) => s.clone(),
        other => panic!("a pool text {other:?}"),
    }
}

/// `gen_history_render._draw`: one history, its draws in that order.
fn draw(rng: &mut SplitMix, pools: &serde_json::Map<String, Value>) -> RecordHistory {
    let pool = |name: &str| -> Vec<PyValue> {
        pools[name]
            .as_array()
            .expect("a pool")
            .iter()
            .map(neutral)
            .collect()
    };
    let record_id = str_of(rng.choice(&pool("ids")));
    let record_kind = opt_str(rng.choice(&pool("rkinds"))).map(|s| leak(&s));
    let state = leak(&opt_str(rng.choice(&pool("states"))).expect("a state"));
    let provenance = opt_str(rng.choice(&pool("provs")));
    let key_present = rng.below(2) == 1;
    let mut rows = Vec::new();
    for _ in 0..rng.below(4) {
        let seq = rng.below(1000) as i64;
        let ts = opt_str(rng.choice(&pool("ts"))).expect("ts");
        let session = opt_str(rng.choice(&pool("sessions")));
        let kind = opt_str(rng.choice(&pool("kinds"))).expect("kind");
        let role = leak(&opt_str(rng.choice(&pool("roles"))).expect("role"));
        let action = opt_str(rng.choice(&pool("actions"))).expect("action");
        let verified = *rng.choice(&[Some(true), Some(false), None]);
        let actor = Some(rng.choice(&pool("actors")).clone()).filter(|a| *a != PyValue::None);
        let actor_source = opt_str(rng.choice(&pool("sources"))).map(|s| leak(&s));
        let changed = (0..rng.below(3))
            .map(|_| str_of(rng.choice(&pool("changed"))))
            .collect();
        let mut detail: Vec<(PyValue, PyValue)> = Vec::new();
        for _ in 0..rng.below(4) {
            let name = rng.choice(&pool("names")).clone();
            let value = rng.choice(&pool("values")).clone();
            match detail.iter_mut().find(|(k, _)| *k == name) {
                Some(slot) => slot.1 = value,
                None => detail.push((name, value)),
            }
        }
        let diff = match rng.choice(&pool("diffs")) {
            PyValue::None => None,
            other => Some(str_of(other)),
        };
        rows.push(HistoryRow {
            seq,
            ts,
            session,
            kind,
            role,
            action,
            verified,
            actor,
            actor_source,
            changed,
            detail,
            diff,
        });
    }
    RecordHistory {
        record_id,
        record_kind,
        state,
        provenance,
        key_present,
        rows,
    }
}

/// UTF-8 with surrogates passed, as the generator encodes the blob.
fn utf8_pass(text: &[u32], out: &mut Vec<u8>) {
    for &c in text {
        match char::from_u32(c) {
            Some(ch) => out.extend_from_slice(ch.encode_utf8(&mut [0u8; 4]).as_bytes()),
            None => out.extend_from_slice(&[
                0xE0 | (c >> 12) as u8,
                0x80 | ((c >> 6) & 0x3F) as u8,
                0x80 | (c & 0x3F) as u8,
            ]),
        }
    }
}

fn answer(got: Result<PyStr, &'static str>) -> PyStr {
    got.unwrap_or_else(|e| unicode::cps(&format!("!{e}")))
}

fn renders(h: &RecordHistory, out: &mut Vec<u8>) {
    for got in [
        render_text(h, false).map_err(RenderError::name),
        render_text(h, true).map_err(RenderError::name),
        render_json(h, false).map_err(|e| e.name()),
        render_json(h, true).map_err(|e| e.name()),
    ] {
        utf8_pass(&answer(got), out);
        out.push(0);
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn check(case: &Value) {
    match case["f"].as_str().expect("f") {
        "actor" => expect(
            case,
            "r",
            actor_text(&neutral(&case["a"])).map_err(RenderError::name),
        ),
        "value" => expect(
            case,
            "r",
            value_text(&neutral(&case["v"])).map_err(|e| e.name()),
        ),
        "render" => {
            let h = history_of(&case["h"]);
            expect(case, "t", render_text(&h, false).map_err(RenderError::name));
            expect(case, "td", render_text(&h, true).map_err(RenderError::name));
            expect(case, "j", render_json(&h, false).map_err(|e| e.name()));
            expect(case, "jd", render_json(&h, true).map_err(|e| e.name()));
        }
        "sweep" => {
            let mut rng = SplitMix(case["seed"].as_u64().expect("seed"));
            let pools = case["pools"].as_object().expect("pools");
            let count = case["count"].as_u64().expect("count") as usize;
            assert!(count > 0);
            let mut blob = Vec::new();
            for _ in 0..count {
                renders(&draw(&mut rng, pools), &mut blob);
            }
            assert_eq!(
                hex(&Sha256::digest(&blob)),
                case["sha256"].as_str().expect("sha256"),
                "the sweep of {count}"
            );
        }
        other => panic!("unknown case {other}"),
    }
}

#[test]
fn every_history_render_case() {
    let lines = jsonl("history_render.jsonl");
    let cases = &lines[1..];
    let mut ledger = Ledger::new("every_history_render_case", "history_render.jsonl");
    for (i, case) in cases.iter().enumerate() {
        ledger.case(format!("line {}", i + 2), || check(case));
    }
    assert_eq!(Some(cases.len() as u64), lines[0]["cases"].as_u64());
    ledger.finish(cases.len());
}

/// `gen_history_render.unicode_history`.
fn unicode_history(text: &[u32]) -> RecordHistory {
    let s = |t: &str| PyValue::str(t);
    RecordHistory {
        record_id: text.to_vec(),
        record_kind: Some("memory"),
        state: "active",
        provenance: Some("p".to_owned()),
        key_present: true,
        rows: vec![HistoryRow {
            seq: 1,
            ts: "2026-01-02T03:04:05Z".to_owned(),
            session: None,
            kind: "memory_put".to_owned(),
            role: "record",
            action: "created".to_owned(),
            verified: Some(true),
            actor: Some(PyValue::Dict(vec![(
                s("client"),
                PyValue::Str(text.to_vec()),
            )])),
            actor_source: Some(ACTOR_FROM_EVENT),
            changed: vec![text.to_vec()],
            detail: vec![
                (PyValue::Str(text.to_vec()), PyValue::Str(text.to_vec())),
                (s("w"), PyValue::List(vec![PyValue::Str(text.to_vec())])),
            ],
            diff: Some(text.to_vec()),
        }],
    }
}

#[test]
fn every_code_point_through_render_text() {
    let file = "history_render_unicode.jsonl";
    let lines = jsonl(file);
    let header = &lines[0];
    let chunk = header["chunk"].as_u64().expect("chunk") as u32;
    let block = header["block"].as_u64().expect("block") as u32;
    let rows = &lines[1..];
    assert_eq!(Some(rows.len() as u64), header["cases"].as_u64());
    let mut ledger = Ledger::new("every_code_point_through_render_text", file);
    for r in rows {
        let lo = r["lo"].as_u64().expect("lo") as u32;
        ledger.case(format!("block {lo:#x}"), || {
            let mut blob = Vec::new();
            let mut count = 0;
            for start in (lo..lo + block).step_by(chunk as usize) {
                let text: PyStr = (start..start + chunk).collect();
                if count > 0 {
                    blob.push(0);
                }
                let got = render_text(&unicode_history(&text), true).expect("rendered");
                utf8_pass(&got, &mut blob);
                count += 1;
            }
            assert_eq!(Some(count), r["count"].as_u64(), "block {lo:#x}");
            assert_eq!(
                hex(&Sha256::digest(&blob)),
                r["sha256"].as_str().expect("sha256"),
                "block {lo:#x}"
            );
        });
    }
    ledger.finish(rows.len());
}
