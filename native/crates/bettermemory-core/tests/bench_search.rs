//! A timing, not a check: the corpus of `search_ranker.jsonl`, every query
//! of the file in each of its core modes (keyword, bm25, hybrid, hybrid
//! with rescue expansion) at its `now` and `max_results`, repeated, the
//! query's text in and the ranked hits out, as the reference is timed. The
//! corpus is prepared once before the clock starts (the reference's token
//! cache is filled by an untimed pass). Run it in release mode:
//!
//! ```text
//! cargo test --offline --release --test bench_search -- --ignored --nocapture
//! ```
//!
//! `BM_BENCH_REPS` sets the repetitions (default 5);
//! `native/tools/bench_search.py` times the reference the same way.

mod common;

use std::time::Instant;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::search::{Corpus, Mode, Options, Record};
use bettermemory_core::unicode;
use common::{jsonl, text_field};

#[test]
#[ignore = "a timing; run with --ignored in release mode"]
fn bench_search_ranker_queries() {
    let reps: usize = std::env::var("BM_BENCH_REPS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(5);
    let lines = jsonl("search_ranker.jsonl");
    let header = &lines[0];
    let dt = |v: &serde_json::Value| {
        DateTime::from_isoformat(&unicode::cps(v.as_str().unwrap())).unwrap()
    };
    let records: Vec<Record> = header["memories"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| Record {
            id: m["id"].as_str().unwrap().to_owned(),
            created: dt(&m["created"]),
            updated: dt(&m["updated"]),
            scopes: m["scopes"]
                .as_array()
                .unwrap()
                .iter()
                .map(|s| s.as_str().unwrap().to_owned())
                .collect(),
            body: text_field(m, "body"),
        })
        .collect();
    let now = dt(&header["now"]);
    // Queries as text; the one holding a lone surrogate (none today) would
    // stay as code points.
    let queries: Vec<Result<String, Vec<u32>>> = lines[1..]
        .iter()
        .map(|r| {
            let cps = text_field(r, "query");
            unicode::to_string(&cps).ok_or(cps)
        })
        .collect();
    let modes: Vec<Options> = header["core_modes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|name| {
            let o = &header["modes"][name.as_str().unwrap()];
            Options {
                mode: Mode::parse(o["mode"].as_str().unwrap()).unwrap(),
                max_results: header["max_results"].as_i64().unwrap(),
                rescue_expansion: o.get("rescue_expansion").is_some_and(|v| v == true),
                ..Options::default()
            }
        })
        .collect();
    let corpus = Corpus::new(&records).unwrap();
    let mut hits = 0;
    let start = Instant::now();
    for _ in 0..reps {
        for q in &queries {
            let cps = match q {
                Ok(text) => unicode::cps(text),
                Err(cps) => cps.clone(),
            };
            for opts in &modes {
                hits += corpus.search(&cps, &now, opts).unwrap().len();
            }
        }
    }
    let elapsed = start.elapsed();
    println!(
        "rust search: {} records, {} queries x {} modes x {reps} reps = {:.3} s, {:.2} ms per pass, {hits} hits",
        records.len(),
        queries.len(),
        modes.len(),
        elapsed.as_secs_f64(),
        elapsed.as_secs_f64() * 1000.0 / reps as f64
    );
}
