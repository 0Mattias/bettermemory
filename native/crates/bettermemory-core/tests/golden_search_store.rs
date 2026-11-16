//! `search_store.jsonl`: search through a store file, by caller. Each
//! store the reference searched is rebuilt from the schema and the rows
//! its header carries (`gen_store_path._store_dump`), with the SQL its
//! case applies after them, opened read only, and every query is pooled
//! and ranked natively (`store::pool`) for the offline run and each
//! caller's admission, the admission read as data from the header. Each
//! case compares the pool (ids, or row numbers on a ``store`` line), each
//! call of the pool's statistics (terms, size, document frequencies, the
//! average length's IEEE-754 bits as text: no float goes through
//! serde_json's parser), and the hits (ids, order, both scores' bits,
//! relevance, match terms; on the main store's lines also the reprs and
//! rank). The ``hostile`` lines are compared the same way, and the
//! ``store`` lines (the larger store's filters, pool sizes and result
//! widths; the small stores whose digest is current, behind, under other
//! rules, gone, or holds values `int()` reads or refuses) by
//! `every_store_line`. `offline_runs_equal_the_in_memory_ranking` holds the
//! offline hits to `search.jsonl`'s in-memory ranking of the same records,
//! as the reference's `in_memory_equal` says they are.

mod common;

use std::collections::HashMap;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::search::{CorpusStats, Hit, Mode};
use bettermemory_core::store::pool::{self, Admission, Filters, PoolError, PoolReader};
use bettermemory_core::store::{StoreError, StoreFile};
use bettermemory_core::{float_repr, unicode};
use common::parity::Ledger;
use common::{jsonl, stores};
use serde_json::{json, Value};

const FILE: &str = "search_store.jsonl";
/// Run lines: 27 queries in 3 modes. Cases per run line: the offline run
/// and 3 callers on 2 surfaces. Hostile lines: 12 queries in 3 modes, 2
/// runs each. Store lines: the larger store's 9 filter sets for 3 callers,
/// 21 (size, width, query, mode) runs and 3 cut pools; 12 small stores'
/// 2 queries for 2 callers.
const RUN_LINES: usize = 81;
const RUNS_PER_LINE: usize = 7;
const HOSTILE_LINES: usize = 36;
const HOSTILE_RUNS: usize = 2;
const BIG_LINES: usize = 27 + 21 + 3;
const SMALL_STORES: usize = 19;
const SMALL_LINES: usize = SMALL_STORES * 4;

fn bits(x: f64) -> String {
    format!("{:016x}", x.to_bits())
}

fn hits_json(hits: &[Hit]) -> Value {
    Value::Array(
        hits.iter()
            .enumerate()
            .map(|(i, h)| {
                json!({
                    "rank": i + 1,
                    "id": h.id,
                    "score": float_repr::repr(h.score),
                    "bits": bits(h.score),
                    "raw_score": float_repr::repr(h.raw_score),
                    "raw_bits": bits(h.raw_score),
                    "relevance": h.relevance,
                    "match_terms": h.match_terms,
                })
            })
            .collect(),
    )
}

fn sorted_object(map: &HashMap<String, i64>) -> Value {
    Value::Object(map.iter().map(|(k, v)| (k.clone(), json!(v))).collect())
}

fn stats_json(terms: &[String], found: &Option<CorpusStats>) -> Value {
    let mut out = vec![json!(terms)];
    match found {
        None => out.push(Value::Null),
        Some(s) => {
            out.push(json!(s.size));
            out.push(sorted_object(&s.body_df));
            out.push(sorted_object(&s.scope_df));
            out.push(match s.avgdl {
                None => Value::Null,
                Some(a) => json!(bits(a)),
            });
        }
    }
    Value::Array(out)
}

fn error_name(e: &PoolError) -> String {
    match e {
        PoolError::Store(StoreError::Py(p)) => p.name().to_owned(),
        PoolError::Search(s) => s.name().to_owned(),
        other => format!("{other:?}"),
    }
}

fn admission(data: &Value) -> Admission {
    Admission {
        projects: data["projects"]
            .as_object()
            .expect("projects")
            .iter()
            .map(|(k, v)| (k.clone(), v.as_bool().expect("bool")))
            .collect(),
        global_rows: data["global_rows"]
            .as_array()
            .expect("global_rows")
            .iter()
            .map(|pair| {
                let scopes = pair[0]
                    .as_array()
                    .expect("scopes")
                    .iter()
                    .map(|s| s.as_str().expect("scope").to_owned())
                    .collect();
                (scopes, pair[1].as_bool().expect("bool"))
            })
            .collect(),
    }
}

/// A store of the header, rebuilt in a scratch directory.
struct Fixture {
    _dir: stores::Scratch,
    store: StoreFile,
    now: DateTime,
    max_results: i64,
    fingerprint: String,
    admissions: HashMap<String, Admission>,
}

fn json_value(v: &Value) -> rusqlite::types::Value {
    match v {
        Value::Null => rusqlite::types::Value::Null,
        Value::Number(n) => rusqlite::types::Value::Integer(n.as_i64().expect("an integer")),
        Value::String(t) => rusqlite::types::Value::Text(t.clone()),
        other => panic!("a column value: {other}"),
    }
}

/// `gen_store_path.rebuild_store`: the schema's tables, the rows, the rest
/// of the schema, the journal mode, then `recipe`.
fn rebuild(schema: &Value, tables: &Value, recipe: &[Value], path: &std::path::Path) {
    let conn = rusqlite::Connection::open(path).expect("create");
    conn.execute_batch(&format!(
        "PRAGMA page_size = {}",
        schema["page_size"].as_i64().expect("page_size")
    ))
    .expect("page size");
    for sql in schema["tables"].as_array().expect("tables") {
        conn.execute_batch(sql.as_str().expect("sql"))
            .expect("table");
    }
    for (table, content) in tables.as_object().expect("tables") {
        let rows = content["rows"].as_array().expect("rows");
        if rows.is_empty() {
            continue;
        }
        let columns: Vec<String> = content["columns"]
            .as_array()
            .expect("columns")
            .iter()
            .map(|c| format!("\"{}\"", c.as_str().expect("column")))
            .collect();
        let marks = vec!["?"; columns.len()].join(", ");
        let sql = format!(
            "INSERT INTO \"{table}\"({}) VALUES ({marks})",
            columns.join(", ")
        );
        let mut statement = conn.prepare(&sql).expect("insert");
        for row in rows {
            let values: Vec<rusqlite::types::Value> = row
                .as_array()
                .expect("row")
                .iter()
                .map(json_value)
                .collect();
            statement
                .execute(rusqlite::params_from_iter(values))
                .expect("row");
        }
    }
    for sql in schema["rest"].as_array().expect("rest") {
        conn.execute_batch(sql.as_str().expect("sql"))
            .expect("statement");
    }
    let mode = schema["journal_mode"].as_str().expect("journal_mode");
    conn.query_row(&format!("PRAGMA journal_mode = {mode}"), [], |_| Ok(()))
        .expect("journal mode");
    for sql in recipe {
        conn.execute_batch(sql.as_str().expect("sql"))
            .expect("recipe");
    }
}

fn admissions(data: &Value) -> HashMap<String, Admission> {
    data.as_object()
        .expect("admissions")
        .iter()
        .map(|(k, v)| (k.clone(), admission(v)))
        .collect()
}

fn open(
    header: &Value,
    tables: &Value,
    recipe: &[Value],
    admissions: HashMap<String, Admission>,
) -> Fixture {
    assert_eq!(header["fixture"], "search_store");
    let dir = stores::scratch("search-store");
    let case = dir.0.join("store");
    std::fs::create_dir_all(&case).expect("dir");
    rebuild(
        &header["schema"],
        tables,
        recipe,
        &case.join("memory.sqlite"),
    );
    let store = StoreFile::open(&case.join("memory.sqlite")).expect("the store opens");
    Fixture {
        _dir: dir,
        store,
        now: DateTime::from_isoformat(&unicode::cps(header["now"].as_str().expect("now")))
            .expect("now"),
        max_results: header["max_results"].as_i64().expect("max_results"),
        fingerprint: header["digest_fingerprint"]
            .as_str()
            .expect("fingerprint")
            .to_owned(),
        admissions,
    }
}

/// The main store.
fn fixture(header: &Value) -> Fixture {
    open(
        header,
        &header["store"]["tables"],
        &[],
        admissions(&header["admissions"]),
    )
}

/// One of the header's other stores.
fn named_store(header: &Value, name: &str) -> Fixture {
    let entry = &header["stores"][name];
    open(
        header,
        &entry["tables"],
        entry["recipe"].as_array().expect("recipe"),
        admissions(&entry["admissions"]),
    )
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .expect("a list")
        .iter()
        .map(|s| s.as_str().expect("a string").to_owned())
        .collect()
}

impl Fixture {
    /// One run as `gen_store_path._run` records it.
    fn run(&self, reader: &PoolReader<'_>, query: &str, mode: &str, who: &str) -> Value {
        let admission = (who != "offline").then(|| &self.admissions[who]);
        let filters = Filters {
            admission,
            ..Filters::default()
        };
        let mode = Mode::parse(mode).expect("a mode");
        let drawn =
            reader.resolve_search_pool(query, &filters, pool::POOL_SIZE, Some(&self.fingerprint));
        let drawn = match drawn {
            Ok(p) => p,
            Err(e) => return json!({"error": error_name(&e)}),
        };
        let mut stats = Vec::new();
        let ranked = drawn.rank(query, &self.now, mode, self.max_results, &mut |t, s| {
            stats.push(stats_json(t, s))
        });
        match ranked {
            Ok(hits) => json!({
                "pool": drawn.ids(),
                "hits": hits_json(&hits),
                "stats": stats,
            }),
            Err(e) => json!({"error": error_name(&e)}),
        }
    }
}

impl Fixture {
    /// One ``store`` line's run, as `gen_store_path._run` records it with
    /// `compact`.
    fn run_line(&self, reader: &PoolReader<'_>, line: &Value) -> Value {
        let who = line["who"].as_str().expect("who");
        let f = &line["filters"];
        let scopes = f.get("scopes").map(strings);
        let excluded = f.get("excluded").map(strings).unwrap_or_default();
        let filters = Filters {
            scopes: scopes.as_deref(),
            excluded: &excluded,
            admission: (who != "offline").then(|| &self.admissions[who]),
            client: f.get("client").and_then(Value::as_str),
            model: f.get("model").and_then(Value::as_str),
        };
        let query = line["query"].as_str().expect("query");
        let size = line["size"].as_u64().expect("size") as usize;
        let width = line["max_results"].as_i64().expect("max_results");
        let mode = Mode::parse(line["mode"].as_str().expect("mode")).expect("a mode");
        let drawn = match reader.resolve_search_pool(query, &filters, size, Some(&self.fingerprint))
        {
            Ok(p) => p,
            Err(e) => return json!({"error": error_name(&e)}),
        };
        let mut stats = Vec::new();
        let ranked = drawn.rank(query, &self.now, mode, width, &mut |t, s| {
            stats.push(stats_json(t, s))
        });
        match ranked {
            Ok(hits) => json!({
                "pool": drawn.rowids,
                "hits": hits
                    .iter()
                    .map(|h| json!([h.id, bits(h.score), bits(h.raw_score), h.relevance, h.match_terms]))
                    .collect::<Vec<_>>(),
                "stats": stats,
            }),
            Err(e) => json!({"error": error_name(&e)}),
        }
    }
}

#[test]
fn every_store_line() {
    let lines = jsonl(FILE);
    let header = &lines[0];
    let mut ledger = Ledger::new("every_store_line", FILE);
    let mut failures = Vec::new();
    let (mut big, mut small, mut in_file) = (0, 0, 0);
    let names: Vec<&String> = header["stores"]
        .as_object()
        .expect("stores")
        .keys()
        .collect();
    assert_eq!(names.len(), SMALL_STORES + 1);
    for name in names {
        let fx = named_store(header, name);
        let reader = PoolReader::new(&fx.store);
        for line in lines[1..]
            .iter()
            .filter(|l| l.get("store").and_then(Value::as_str) == Some(name.as_str()))
        {
            in_file += 1;
            if name == "big" {
                big += 1;
            } else {
                small += 1;
            }
            let got = fx.run_line(&reader, line);
            let expected = &line["result"];
            let label = format!(
                "{name} {:?} [{}] {} {} size {} width {}: expected {expected} got {got}",
                line["query"],
                line["mode"],
                line["who"],
                line["filters"],
                line["size"],
                line["max_results"]
            );
            if ledger.differs_fatally(&label, &got == expected) {
                failures.push(label);
            }
        }
    }
    assert!(
        failures.is_empty(),
        "{} failures:\n{}",
        failures.len(),
        failures
            .iter()
            .take(10)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
    assert_eq!(big, BIG_LINES);
    assert_eq!(small, SMALL_LINES);
    ledger.finish(in_file);
}

fn query_of(line: &Value) -> String {
    let text = line["query"].as_str().expect("query");
    text.repeat(line.get("times").and_then(Value::as_u64).unwrap_or(1) as usize)
}

#[test]
fn every_query_every_caller_pool_and_hits() {
    let lines = jsonl(FILE);
    let fx = fixture(&lines[0]);
    let reader = PoolReader::new(&fx.store);
    let mut ledger = Ledger::new("every_query_every_caller_pool_and_hits", FILE);
    let mut failures = Vec::new();
    let (mut runs, mut hostile, mut in_file) = (0, 0, 0);
    for line in lines[1..].iter().filter(|l| l.get("store").is_none()) {
        let query = query_of(line);
        let mode = line["mode"].as_str().expect("mode");
        let is_hostile = line.get("hostile").is_some();
        if is_hostile {
            hostile += 1;
        } else {
            runs += 1;
        }
        for (who, expected) in line.as_object().expect("line") {
            if !expected.is_object() {
                continue;
            }
            in_file += 1;
            let got = fx.run(&reader, &query, mode, who);
            let shown: String = query.chars().take(80).collect();
            let label = format!("{shown:?} [{mode}] {who}: expected {expected} got {got}");
            if ledger.differs_fatally(&label, &got == expected) {
                failures.push(label);
            }
        }
    }
    assert!(
        failures.is_empty(),
        "{} failures:\n{}",
        failures.len(),
        failures
            .iter()
            .take(10)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
    assert_eq!(runs, RUN_LINES);
    assert_eq!(hostile, HOSTILE_LINES);
    common::assert_count(
        FILE,
        in_file,
        RUN_LINES * RUNS_PER_LINE + HOSTILE_LINES * HOSTILE_RUNS,
    );
    ledger.finish(in_file);
}

#[test]
fn offline_runs_equal_the_in_memory_ranking() {
    let lines = jsonl(FILE);
    let fx = fixture(&lines[0]);
    let reader = PoolReader::new(&fx.store);
    let in_memory: HashMap<String, Value> = jsonl("search.jsonl")[1..]
        .iter()
        .map(|l| {
            (
                l["query"].as_str().expect("query").to_owned(),
                l["hits"].clone(),
            )
        })
        .collect();
    let mut ledger = Ledger::new("offline_runs_equal_the_in_memory_ranking", "search.jsonl");
    let mut failures = Vec::new();
    let mut in_file = 0;
    for line in lines[1..]
        .iter()
        .filter(|l| l.get("hostile").is_none() && l.get("store").is_none())
    {
        in_file += 1;
        let query = query_of(line);
        let mode = line["mode"].as_str().expect("mode");
        assert_eq!(line["in_memory_equal"], true, "the reference's own claim");
        let got = fx.run(&reader, &query, mode, "offline");
        let expected = &in_memory[&query][mode];
        let label = format!(
            "{query:?} [{mode}]: in memory {expected} got {}",
            got["hits"]
        );
        if ledger.differs_fatally(&label, &got["hits"] == expected) {
            failures.push(label);
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
    ledger.finish(in_file);
    assert_eq!(in_file, RUN_LINES);
}

#[test]
fn the_width_clamp() {
    assert_eq!(pool::clamp_search_width(0), 1);
    assert_eq!(pool::clamp_search_width(-5), 1);
    assert_eq!(pool::clamp_search_width(7), 7);
    assert_eq!(pool::clamp_search_width(50), 50);
    assert_eq!(pool::clamp_search_width(51), 50);
    assert_eq!(pool::clamp_search_width(i64::MAX), 50);
}

#[test]
fn a_digest_under_other_rules_gives_no_average() {
    let lines = jsonl(FILE);
    let fx = fixture(&lines[0]);
    let reader = PoolReader::new(&fx.store);
    assert!(reader.digest_current(&fx.fingerprint));
    assert!(!reader.digest_current("other rules"));
    let drawn = reader
        .resolve_search_pool("docker compose", &Filters::default(), pool::POOL_SIZE, None)
        .expect("a pool");
    let stats = drawn
        .statistics(&["docker".to_owned()])
        .expect("statistics")
        .expect("some");
    assert_eq!(stats.avgdl, None);
}

/// A timing, not a check: every run line's query and mode for the offline
/// run and each caller's admission (567 pool-and-rank calls a pass), the
/// query's text in and the ranked hits out, over the rebuilt
/// store opened once. `BM_BENCH_REPS` sets the passes (default 20);
/// `native/tools/bench_search_store.py` times the reference the same way.
///
/// ```text
/// cargo test --offline --release --test golden_search_store -- --ignored --nocapture
/// ```
#[test]
#[ignore = "a timing; run with --ignored in release mode"]
fn bench_pool_and_rank() {
    let reps: usize = std::env::var("BM_BENCH_REPS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(20);
    let lines = jsonl(FILE);
    let fx = fixture(&lines[0]);
    let reader = PoolReader::new(&fx.store);
    let calls: Vec<(String, String, String)> = lines[1..]
        .iter()
        .filter(|l| l.get("hostile").is_none() && l.get("store").is_none())
        .flat_map(|l| {
            let query = query_of(l);
            let mode = l["mode"].as_str().expect("mode").to_owned();
            l.as_object()
                .expect("line")
                .iter()
                .filter(|(_, v)| v.is_object())
                .map(|(who, _)| (query.clone(), mode.clone(), who.clone()))
                .collect::<Vec<_>>()
        })
        .collect();
    let mut passes = Vec::with_capacity(reps);
    let mut hits = 0usize;
    for _ in 0..reps {
        let start = std::time::Instant::now();
        for (query, mode, who) in &calls {
            let admission = (who != "offline").then(|| &fx.admissions[who]);
            let filters = Filters {
                admission,
                ..Filters::default()
            };
            let drawn = reader
                .resolve_search_pool(query, &filters, pool::POOL_SIZE, Some(&fx.fingerprint))
                .expect("a pool");
            let mode = Mode::parse(mode).expect("a mode");
            hits += drawn
                .rank(query, &fx.now, mode, fx.max_results, &mut |_, _| {})
                .expect("hits")
                .len();
        }
        passes.push(start.elapsed().as_secs_f64() * 1000.0);
    }
    passes.sort_by(f64::total_cmp);
    let mut drawing = Vec::with_capacity(reps);
    let mut drawn_records = 0usize;
    for _ in 0..reps {
        let start = std::time::Instant::now();
        for (query, _, who) in &calls {
            let admission = (who != "offline").then(|| &fx.admissions[who]);
            let filters = Filters {
                admission,
                ..Filters::default()
            };
            let drawn = reader
                .resolve_search_pool(query, &filters, pool::POOL_SIZE, Some(&fx.fingerprint))
                .expect("a pool");
            drawn_records += drawn.records.len();
        }
        drawing.push(start.elapsed().as_secs_f64() * 1000.0);
    }
    drawing.sort_by(f64::total_cmp);
    println!(
        "native pool drawing alone: median {:.2} ms a pass, {drawn_records} records",
        drawing[reps / 2]
    );
    println!(
        "native pool-and-rank: {} calls a pass, {reps} passes, median {:.2} ms a pass \
         (min {:.2}, max {:.2}), {hits} hits",
        calls.len(),
        passes[reps / 2],
        passes[0],
        passes[reps - 1]
    );
}

/// No panic on what a caller can type: every printable ASCII character
/// and the FTS5 operators alone, doubled, between words and quoted, control
/// characters, and a long run of distinct terms. The reference's answers
/// for the fixture's hostile queries are pinned above; these are only
/// held to an answer or a refusal.
#[test]
fn awkward_queries_never_panic() {
    let lines = jsonl(FILE);
    let fx = fixture(&lines[0]);
    let reader = PoolReader::new(&fx.store);
    let mut queries: Vec<String> = Vec::new();
    for b in 0u8..128 {
        let c = char::from(b);
        queries.push(c.to_string());
        queries.push(format!("{c}{c}"));
        queries.push(format!("docker {c} compose"));
        queries.push(format!("\"docker{c}\""));
    }
    for op in [
        "AND", "OR", "NOT", "NEAR", "NEAR(", "^", "*", ":", "+", "-", "\"",
    ] {
        queries.push(format!("{op} docker {op}"));
    }
    queries.push((0..3000).map(|i| format!("term{i} ")).collect());
    queries.push("\u{0}\u{1}\u{7f}\u{feff}\u{200b}".to_owned());
    for query in &queries {
        for mode in ["hybrid", "bm25", "keyword"] {
            for who in ["offline", "alpha/auto_scope", "plain/unscoped"] {
                let got = fx.run(&reader, query, mode, who);
                assert!(got.get("hits").is_some() || got.get("error").is_some());
            }
        }
    }
}

/// The time of the first pool drawn from the small store whose digest is
/// current, after `n` unseen `memory_delete` rows naming distinct records
/// the store does not hold: the catch-up reads every one.
fn time_catch_up(n: usize) -> std::time::Duration {
    let lines = jsonl(FILE);
    let header = &lines[0];
    let entry = &header["stores"]["digest: current"];
    let recipe = vec![Value::String(format!(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < {n}) \
         INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) \
         SELECT (SELECT MAX(seq) FROM log) + x, '2026-06-01T12:00:00Z', NULL, \
         'memory_delete', '{{\"id\": \"gone-' || x || '\"}}', '', 'm' FROM c"
    ))];
    let fx = open(
        header,
        &entry["tables"],
        &recipe,
        admissions(&entry["admissions"]),
    );
    let reader = PoolReader::new(&fx.store);
    let start = std::time::Instant::now();
    let drawn = reader
        .resolve_search_pool(
            "docker",
            &Filters::default(),
            pool::POOL_SIZE,
            Some(&fx.fingerprint),
        )
        .expect("a pool");
    let stats = drawn
        .statistics(&["docker".to_owned()])
        .expect("statistics")
        .expect("some");
    let took = start.elapsed();
    assert!(stats.avgdl.is_some(), "the digest is brought forward");
    took
}

/// The catch-up's ids are kept in first-seen order with a set beside
/// them: ten times the unseen rows (5,000 and 50,000) cost about ten
/// times the time, not a hundred (searching the list instead took 8 s for
/// 50,000 in a debug build against 0.16 s for 5,000).
#[test]
fn catching_up_is_linear_in_the_unseen_rows() {
    let small = (0..3).map(|_| time_catch_up(5_000)).min().expect("timed");
    let large = (0..3).map(|_| time_catch_up(50_000)).min().expect("timed");
    let bound = small * 10 * 4 + std::time::Duration::from_millis(20);
    assert!(large <= bound, "50k rows {large:?}, 5k rows {small:?}");
}
