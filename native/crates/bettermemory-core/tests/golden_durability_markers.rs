//! Every case of `durability_markers.jsonl` (not gated) and
//! `durability_markers_unicode.jsonl` (gated on the Unicode version)
//! against the native `durability` transient markers, unit 2: the tables,
//! each pattern's text and flags, `find_transient_markers`,
//! `_anchored_new_reference`, and the seeded sweep by count and digest.
//! The spelling of rows is the generator's
//! (`tests/contract/gen_durability.py`).

mod common;

use std::collections::BTreeMap;

use bettermemory_core::durability as d;
use bettermemory_core::pyre::regex::Regex;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode;
use common::parity::Ledger;
use serde_json::Value;
use sha2::{Digest, Sha256};

fn text(record: &Value, name: &str) -> PyStr {
    common::text_field(record, name)
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| s.as_str().unwrap().to_owned())
        .collect()
}

fn hits(body: &[u32]) -> Vec<(PyStr, PyStr)> {
    d::find_transient_markers(body)
        .expect("every pattern builds")
        .into_iter()
        .map(|h| (h.marker, h.snippet))
        .collect()
}

fn want_hits(v: &Value) -> Vec<(PyStr, PyStr)> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|h| (unicode::cps(h["m"].as_str().unwrap()), text(h, "s")))
        .collect()
}

fn check_tables(r: &Value) {
    assert_eq!(strings(&r["phrases"]), d::TRANSIENT_PHRASE_MARKERS);
    let mut titlecase = d::TITLECASE_SKIP_MARKERS.to_vec();
    titlecase.sort_unstable();
    assert_eq!(strings(&r["titlecase"]), titlecase);
    let overrides: Vec<&str> = d::PATTERN_OVERRIDES.iter().map(|(p, _, _)| *p).collect();
    assert_eq!(strings(&r["overrides"]), overrides);
    assert_eq!(r["in_flight"].as_str().unwrap(), d::IN_FLIGHT_WORK_OBJECT);
    assert_eq!(r["as_of"].as_str().unwrap(), d::AS_OF_DATE_MARKER);
}

fn check_pattern(r: &Value) {
    let id = r["id"].as_str().unwrap();
    let (pattern, flags) = if id == "_AS_OF_DATE_RE" {
        (unicode::cps(d::AS_OF_DATE_RE.0), d::AS_OF_DATE_RE.1)
    } else {
        assert!(d::TRANSIENT_PHRASE_MARKERS.contains(&id), "no phrase {id}");
        d::phrase_pattern(id)
    };
    assert_eq!(pattern, unicode::cps(r["p"].as_str().unwrap()), "{id}");
    let built = Regex::new(&pattern, flags).expect("the engine builds it");
    assert_eq!(u64::from(built.flags()), r["f"].as_u64().unwrap(), "{id}");
}

fn check_anchored(r: &Value) {
    let t = text(r, "t");
    let s = r["s"].as_u64().unwrap() as usize;
    assert_eq!(
        d::anchored_new_reference(&t, s).expect("the vocabulary builds"),
        r["o"].as_bool().unwrap()
    );
}

fn lcg(state: u64) -> u64 {
    state
        .wrapping_mul(6364136223846793005)
        .wrapping_add(1442695040888963407)
}

fn pick(state: u64, len: usize) -> usize {
    ((state >> 33) % len as u64) as usize
}

/// The generator's `marker_sweep_bodies`.
fn sweep_bodies(r: &Value) -> Vec<PyStr> {
    let list = |name: &str| -> Vec<PyStr> {
        r[name]
            .as_array()
            .unwrap()
            .iter()
            .map(|w| unicode::cps(w.as_str().unwrap()))
            .collect()
    };
    let (words, seps) = (list("words"), list("seps"));
    let most = r["most"].as_u64().unwrap();
    let count = r["count"].as_u64().unwrap();
    let mut state = r["seed"].as_u64().unwrap();
    (0..count)
        .map(|_| {
            state = lcg(state);
            let n = 1 + (state >> 33) % most;
            let mut body = Vec::new();
            for _ in 0..n {
                state = lcg(state);
                body.extend_from_slice(&words[pick(state, words.len())]);
                state = lcg(state);
                body.extend_from_slice(&seps[pick(state, seps.len())]);
            }
            body
        })
        .collect()
}

fn check_sweep(r: &Value) {
    let bodies = sweep_bodies(r);
    let mut h = Sha256::new();
    let mut markers: BTreeMap<String, u64> = BTreeMap::new();
    for body in &bodies {
        for (marker, snippet) in hits(body) {
            let marker = unicode::to_string(&marker).expect("a marker name is text");
            *markers.entry(marker.clone()).or_default() += 1;
            let snippet =
                unicode::to_string(&snippet).expect("the sweep's words hold no surrogate");
            h.update(format!("{marker}|{snippet}\n").as_bytes());
        }
        h.update([0u8]);
    }
    assert_eq!(bodies.len() as u64, r["count"].as_u64().unwrap());
    let want: BTreeMap<String, u64> = r["markers"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.as_u64().unwrap()))
        .collect();
    assert_eq!(markers, want, "hits per marker");
    assert_eq!(common::hex(&h.finalize()), r["sha"].as_str().unwrap());
}

fn check(r: &Value) {
    match r["k"].as_str().unwrap() {
        "tables" => check_tables(r),
        "pat" => check_pattern(r),
        "hits" => assert_eq!(hits(&text(r, "t")), want_hits(&r["o"])),
        "anc" => check_anchored(r),
        "sweep" => check_sweep(r),
        other => panic!("unknown row kind {other}"),
    }
}

fn run(test: &str, file: &str) -> Vec<String> {
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let mut ledger = Ledger::new(test, file);
    for (i, r) in rows.iter().enumerate() {
        let shown: String = r.to_string().chars().take(200).collect();
        ledger.case(format!("line {} {shown}", i + 2), || check(r));
    }
    ledger.finish(rows.len());
    rows.iter()
        .filter(|r| r["k"] == "pat")
        .map(|r| r["id"].as_str().unwrap().to_owned())
        .collect()
}

#[test]
fn durability_markers_every_case() {
    let ids = run("durability_markers_every_case", "durability_markers.jsonl");
    let mut mine: Vec<&str> = d::TRANSIENT_PHRASE_MARKERS.to_vec();
    mine.push("_AS_OF_DATE_RE");
    assert_eq!(ids, mine, "the file pins every pattern, in order");
}

#[test]
fn durability_markers_unicode_every_case() {
    run(
        "durability_markers_unicode_every_case",
        "durability_markers_unicode.jsonl",
    );
}
