//! Every query of `search.jsonl` and `search_ranker.jsonl` in every mode the
//! file records: hit ids in order, the returned and the unrounded score's
//! bits and repr, the relevance label and the match terms; a refused mode,
//! its exception type. `search.jsonl`'s `fts` field (FTS5's `bm25()` over
//! SQLite) is not this crate's and is not read; its `match_query` is the
//! lexicon's and `golden_lexicon` pins it, but it is checked here too.
//! `search_ranker.jsonl` writes each hit without what follows from its bits
//! (rank, reprs) and has no `match_query`; its size is pinned as literals.
//! The word tables in `search_ranker.jsonl`'s header are checked against
//! this crate's copies.

mod common;

use std::collections::{HashMap, HashSet};

use bettermemory_core::datetime::DateTime;
use bettermemory_core::search::{
    expansion, lane, Corpus, CorpusStats, Hit, Mode, Options, Record, SearchError,
};
use bettermemory_core::{float_repr, lexicon, unicode};
use common::{assert_count, jsonl, text_field};
use serde_json::{json, Value};

fn dt(text: &str) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(text)).expect("an isoformat")
}

fn records(header: &Value) -> Vec<Record> {
    header["memories"]
        .as_array()
        .expect("memories")
        .iter()
        .map(|m| Record {
            id: m["id"].as_str().unwrap().to_owned(),
            created: dt(m["created"].as_str().unwrap()),
            updated: dt(m["updated"].as_str().unwrap()),
            scopes: m["scopes"]
                .as_array()
                .unwrap()
                .iter()
                .map(|s| s.as_str().unwrap().to_owned())
                .collect(),
            body: text_field(m, "body"),
        })
        .collect()
}

fn bits(x: f64) -> String {
    format!("{:016x}", x.to_bits())
}

/// The hits as `_common.hit_records` writes them (`search.jsonl`), or, with
/// `compact`, as `search_ranker.jsonl` does: id, both scores' bits,
/// relevance and match terms, in rank order.
fn as_json(result: &Result<Vec<Hit>, SearchError>, compact: bool) -> Value {
    match result {
        Err(e) => json!({"error": e.name()}),
        Ok(hits) => Value::Array(
            hits.iter()
                .enumerate()
                .map(|(i, h)| {
                    if compact {
                        return json!({
                            "id": h.id,
                            "bits": bits(h.score),
                            "raw_bits": bits(h.raw_score),
                            "relevance": h.relevance,
                            "match_terms": h.match_terms,
                        });
                    }
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
        ),
    }
}

/// The ``*_stats`` modes' provider, as `gen_search_ranker._provider`:
/// document frequencies and mean content length over the whole corpus.
struct WholeCorpus {
    streams: Vec<(HashSet<String>, HashSet<String>)>,
    avgdl: f64,
}

impl WholeCorpus {
    fn new(records: &[Record]) -> WholeCorpus {
        let mut total = 0usize;
        let streams = records
            .iter()
            .map(|r| {
                let content =
                    lexicon::strip_stopwords(&lexicon::expand_kebab(&lexicon::tokenize(&r.body)));
                total += content.len();
                let content: HashSet<String> = content.into_iter().collect();
                let mut union = content.clone();
                for s in &r.scopes {
                    union.extend(lexicon::expand_kebab(&lexicon::tokenize_str(s)));
                }
                (content, union)
            })
            .collect();
        WholeCorpus {
            streams,
            avgdl: total as f64 / records.len() as f64,
        }
    }

    fn provide(&self, terms: &[String]) -> Option<CorpusStats> {
        let mut stats = CorpusStats {
            size: self.streams.len() as i64,
            avgdl: Some(self.avgdl),
            ..CorpusStats::default()
        };
        for t in terms {
            let b = self.streams.iter().filter(|(c, _)| c.contains(t)).count() as i64;
            let s = self.streams.iter().filter(|(_, u)| u.contains(t)).count() as i64;
            if b > 0 {
                stats.body_df.insert(t.clone(), b);
            }
            if s > 0 {
                stats.scope_df.insert(t.clone(), s);
            }
        }
        Some(stats)
    }
}

fn strings(v: &Value) -> Option<Vec<String>> {
    v.as_array()
        .map(|a| a.iter().map(|s| s.as_str().unwrap().to_owned()).collect())
}

/// Run one mode's keyword arguments, as `gen_search_ranker._run` passes
/// them.
fn run(
    corpus: &Corpus,
    whole: &WholeCorpus,
    query: &[u32],
    header: &Value,
    options: &Value,
) -> Result<Vec<Hit>, SearchError> {
    let mode = Mode::parse(options["mode"].as_str().unwrap())?;
    let now = dt(options
        .get("now")
        .and_then(Value::as_str)
        .unwrap_or_else(|| header["now"].as_str().unwrap()));
    let scopes = options.get("scopes").and_then(strings);
    let excluded = options.get("excluded_scopes").and_then(strings);
    let provide = |terms: &[String]| whole.provide(terms);
    let opts = Options {
        mode,
        max_results: options
            .get("max_results")
            .and_then(Value::as_i64)
            .unwrap_or_else(|| header["max_results"].as_i64().unwrap()),
        half_life_days: options
            .get("half_life_days")
            .and_then(Value::as_f64)
            .unwrap_or_else(|| header["half_life_days"].as_f64().unwrap()),
        rrf_k: options.get("rrf_k").and_then(Value::as_i64).unwrap_or(60),
        conversational: options
            .get("conversational")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        rescue_expansion: options
            .get("rescue_expansion")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        allow_empty_query: options
            .get("allow_empty_query")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        scopes: scopes.as_deref(),
        excluded_scopes: excluded.as_deref(),
        corpus_stats_provider: if options.get("provider").and_then(Value::as_bool) == Some(true) {
            Some(&provide)
        } else {
            None
        },
    };
    corpus.search(query, &now, &opts)
}

/// Check every query record of `name` in every mode it lists; return the
/// number of cases checked and the number in the file.
fn check_file(name: &str, fixture: &str) -> (usize, usize) {
    check_lines(name, &jsonl(name), fixture)
}

fn check_lines(name: &str, lines: &[Value], fixture: &str) -> (usize, usize) {
    let header = &lines[0];
    assert_eq!(header["fixture"], fixture);
    let recs = records(header);
    let corpus = Corpus::new(&recs).expect("the corpus");
    let whole = WholeCorpus::new(&recs);
    let modes = header["modes"].as_object().expect("modes");
    let mut in_file = 0;
    let mut checked = 0;
    let mut failures = Vec::new();
    for record in &lines[1..] {
        let query = text_field(record, "query");
        // search.jsonl carries the lexicon's match query; the ranker file
        // leaves it to lexicon.jsonl.
        let compact = fixture == "search_ranker";
        if compact {
            assert!(record.get("match_query").is_none());
        } else {
            assert_eq!(
                lexicon::fts_match_query(&query),
                record["match_query"].as_str().unwrap()
            );
        }
        let hits = record["hits"].as_object().expect("hits");
        in_file += hits.len();
        for (mode_name, expected) in hits {
            let got = as_json(
                &run(&corpus, &whole, &query, header, &modes[mode_name]),
                compact,
            );
            if &got != expected {
                failures.push(format!(
                    "{:?} [{mode_name}]\n  expected {expected}\n  got      {got}",
                    unicode::to_string(&query)
                ));
            }
            checked += 1;
        }
    }
    assert!(
        failures.is_empty(),
        "{name}: {} failures:\n{}",
        failures.len(),
        failures
            .iter()
            .take(20)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
    (checked, in_file)
}

#[test]
fn search_every_query_every_mode() {
    let (checked, in_file) = check_file("search.jsonl", "search");
    // 27 queries in five modes.
    assert_eq!(in_file, 27 * 5);
    assert_count("search.jsonl", checked, in_file);
}

const RANKER_RECORDS: usize = 324;
const RANKER_QUERIES: usize = 208;
const RANKER_CASES: usize = 1510;

#[test]
fn search_ranker_every_query_every_mode() {
    let lines = jsonl("search_ranker.jsonl");
    let header = &lines[0];
    let core = header["core_modes"].as_array().unwrap().len();
    let expected: usize = lines[1..]
        .iter()
        .map(|r| core + r["extra"].as_array().unwrap().len())
        .sum();
    let (checked, in_file) = check_file("search_ranker.jsonl", "search_ranker");
    assert_eq!(in_file, expected);
    // The fixture's size, pinned: update these when it grows.
    let counts = &header["counts"];
    assert_eq!(header["memories"].as_array().unwrap().len(), RANKER_RECORDS);
    assert_eq!(lines.len() - 1, RANKER_QUERIES);
    assert_eq!(in_file, RANKER_CASES);
    assert_eq!(counts["records"], RANKER_RECORDS);
    assert_eq!(counts["queries"], RANKER_QUERIES);
    assert_eq!(counts["cases"], RANKER_CASES);
    assert_count("search_ranker.jsonl", checked, in_file);
}

#[test]
fn search_ranker_tables() {
    let lines = jsonl("search_ranker.jsonl");
    let tables = &lines[0]["tables"];
    let t = expansion::tables();
    let mut filler: Vec<&String> = t.filler_stems.iter().collect();
    filler.sort();
    assert_eq!(json!(filler), tables["filler_stems"]);
    let sorted_map = |m: &HashMap<String, Vec<String>>| {
        let mut keys: Vec<&String> = m.keys().collect();
        keys.sort();
        let obj: serde_json::Map<String, Value> =
            keys.into_iter().map(|k| (k.clone(), json!(m[k]))).collect();
        Value::Object(obj)
    };
    assert_eq!(sorted_map(&t.irregular), tables["irregular"]);
    assert_eq!(sorted_map(&t.clippings), tables["clippings"]);
    let synonyms: serde_json::Map<String, Value> = t
        .synonyms
        .iter()
        .map(|(k, v)| (k.clone(), json!(v.iter().collect::<Vec<_>>())))
        .collect();
    assert_eq!(Value::Object(synonyms), tables["synonyms"]);
    assert_eq!(json!(lane::scaffold_stems()), tables["scaffold_stems"]);
    assert_eq!(json!(lane::time_unit_stems()), tables["time_unit_stems"]);
    assert_eq!(json!(lane::MONTH_NAMES), tables["month_names"]);
    assert_eq!(json!(lane::MAY_CONTEXT_WORDS), tables["may_context_words"]);
    assert_eq!(
        json!(lane::MAY_MODAL_FOLLOWERS),
        tables["may_modal_followers"]
    );
}

/// A randomized differential check: each file named in `BM_SEARCH_DIFF`
/// (comma-separated paths), written in `search_ranker.jsonl`'s format by a
/// generator outside the repository.
#[test]
#[ignore]
fn search_differential_files() {
    let paths = std::env::var("BM_SEARCH_DIFF").expect("BM_SEARCH_DIFF");
    for path in paths.split(',') {
        let text = std::fs::read_to_string(path).expect("a file");
        let lines: Vec<Value> = text
            .lines()
            .map(|l| serde_json::from_str(l).expect("a JSON line"))
            .collect();
        let (checked, in_file) = check_lines(path, &lines, "search_ranker");
        eprintln!(
            "{path}: {} queries, {checked} of {in_file} cases checked, all passing",
            lines.len() - 1
        );
        assert_eq!(checked, in_file);
    }
}
