//! The ranker on input no store writes: an empty corpus, timestamps at the
//! ends of the calendar or naming no datetime, enormous bodies and queries,
//! lone surrogates, absurd corpus statistics, extreme arguments. Each gives
//! hits, an empty result or an error, never a panic (the test profile keeps
//! debug assertions, so an overflow panics here too).

use bettermemory_core::datetime::DateTime;
use bettermemory_core::search::{search, Corpus, CorpusStats, Mode, Options, Record, SearchError};
use bettermemory_core::unicode;

fn utc(y: i64, mo: u32, d: u32) -> DateTime {
    DateTime::new(y, mo, d, 0, 0, 0, 0, Some(0)).unwrap()
}

fn record(i: usize, body: &str, created: DateTime) -> Record {
    Record {
        id: format!("01J{i:023}"),
        created,
        updated: created,
        scopes: vec!["projects:alpha-beta".into()],
        body: unicode::cps(body),
    }
}

const MODES: [Mode; 3] = [Mode::Keyword, Mode::Bm25, Mode::Hybrid];

const QUERIES: [&str; 14] = [
    "",
    "the",
    "docker",
    "last week",
    "last year",
    "this week",
    "this month",
    "yesterday",
    "99 years ago",
    "december 9999",
    "january 0000",
    "how many days ago",
    "alpha-beta docker-compose end-to-end",
    "remember stuff toggles splitting",
];

fn all_options(corpus: &Corpus, query: &[u32], now: &DateTime) {
    for mode in MODES {
        for (rescue, conversational, allow_empty) in [
            (false, true, false),
            (true, true, true),
            (false, false, false),
        ] {
            for max_results in [i64::MIN, -1, 0, 1, 5, i64::MAX] {
                let opts = Options {
                    mode,
                    max_results,
                    rescue_expansion: rescue,
                    conversational,
                    allow_empty_query: allow_empty,
                    ..Options::default()
                };
                let _ = corpus.search(query, now, &opts);
            }
        }
    }
}

#[test]
fn empty_corpus_is_empty() {
    let corpus = Corpus::new(&[]).unwrap();
    let now = utc(2026, 6, 1);
    for q in QUERIES {
        for mode in MODES {
            let opts = Options {
                mode,
                allow_empty_query: true,
                ..Options::default()
            };
            assert_eq!(corpus.search(&unicode::cps(q), &now, &opts), Ok(vec![]));
        }
    }
}

#[test]
fn the_ends_of_the_calendar() {
    let first = utc(1, 1, 1);
    let last = DateTime::new(9999, 12, 31, 23, 59, 59, 999_999, Some(-86_399_999_999)).unwrap();
    let records = vec![
        record(0, "docker in the first year", first),
        record(1, "docker in the last year [9999-12-31]", last),
        record(2, "[0001-01-01] docker", utc(2026, 6, 1)),
    ];
    let corpus = Corpus::new(&records).unwrap();
    for now in [
        first,
        last,
        utc(1, 1, 7),
        utc(9999, 12, 27),
        utc(2026, 6, 1),
    ] {
        for q in QUERIES {
            all_options(&corpus, &unicode::cps(q), &now);
        }
    }
    // "last year" at year 1 is ValueError, "yesterday" at its first day
    // OverflowError, as the reference raises them.
    let opts = Options::default();
    let q = |s: &str| unicode::cps(&format!("docker {s}"));
    assert_eq!(
        corpus.search(&q("last year"), &first, &opts),
        Err(SearchError::ValueError)
    );
    assert_eq!(
        corpus.search(&q("yesterday"), &first, &opts),
        Err(SearchError::OverflowError)
    );
    assert_eq!(
        corpus.search(&q("this week"), &utc(9999, 12, 31), &opts),
        Err(SearchError::OverflowError)
    );
}

#[test]
fn fields_that_name_no_datetime_are_refused() {
    let mut bad = utc(2026, 6, 1);
    bad.month = 13;
    assert_eq!(
        Corpus::new(&[record(0, "x", bad)]).err(),
        Some(SearchError::InvalidInput)
    );
    let mut r = record(0, "x", utc(2026, 6, 1));
    r.updated.offset_us = Some(i64::MAX);
    assert_eq!(Corpus::new(&[r]).err(), Some(SearchError::InvalidInput));
    let corpus = Corpus::new(&[record(0, "docker", utc(2026, 6, 1))]).unwrap();
    let mut now = utc(2026, 6, 1);
    now.day = 31;
    assert_eq!(
        corpus.search(&unicode::cps("docker"), &now, &Options::default()),
        Err(SearchError::InvalidInput)
    );
}

#[test]
fn naive_timestamps() {
    let naive = DateTime::new(2026, 5, 1, 0, 0, 0, 0, None).unwrap();
    let corpus = Corpus::new(&[record(0, "docker", naive), record(1, "docker", naive)]).unwrap();
    let opts = Options::default();
    // A naive clock cannot be subtracted from a record's time.
    assert_eq!(
        corpus.search(&unicode::cps("docker"), &naive, &opts),
        Err(SearchError::TypeError)
    );
    assert_eq!(
        corpus
            .search(&unicode::cps("docker"), &utc(2026, 6, 1), &opts)
            .unwrap()
            .len(),
        2
    );
    // Naive and aware among the candidates: refused by the port (the
    // reference raises only where its sort compares such a pair).
    let mixed = Corpus::new(&[
        record(0, "docker", naive),
        record(1, "docker", utc(2026, 5, 1)),
    ])
    .unwrap();
    assert_eq!(
        mixed.search(&unicode::cps("docker"), &utc(2026, 6, 1), &opts),
        Err(SearchError::InvalidInput)
    );
    // A record naive in one field and aware in the other, once it scores.
    let mut own = record(0, "docker", utc(2026, 5, 1));
    own.created = naive;
    let corpus = Corpus::new(&[own]).unwrap();
    assert_eq!(
        corpus.search(&unicode::cps("docker"), &utc(2026, 6, 1), &opts),
        Err(SearchError::TypeError)
    );
    assert_eq!(
        corpus.search(&unicode::cps("nothing"), &utc(2026, 6, 1), &opts),
        Ok(vec![])
    );
}

#[test]
fn enormous_bodies_queries_and_surrogates() {
    let now = utc(2026, 6, 1);
    let mut records = vec![
        record(0, &"docker compose ".repeat(100_000), now),
        record(1, &"x".repeat(300_000), now),
        record(2, &"a-".repeat(50_000), now),
        record(3, &"東京".repeat(50_000), now),
        record(4, "", now),
        record(5, "   ", now),
        record(5, "docker", now),
    ];
    records.push(Record {
        body: vec![
            0xD800, 0x64, 0x6F, 0x63, 0x6B, 0x65, 0x72, 0xDFFF, 0x20, 0x31,
        ],
        ..record(7, "", now)
    });
    let corpus = Corpus::new(&records).unwrap();
    let long_query = "docker ".repeat(100_000) + &"word ".repeat(20_000);
    for q in [
        unicode::cps(&long_query),
        vec![0xD800],
        vec![0xDC00, 0x64, 0x6F, 0x63, 0x6B, 0x65, 0x72],
        unicode::cps("東京"),
        unicode::cps("a-a-a-a"),
        unicode::cps(&"2026-05-03 ".repeat(1000)),
        unicode::cps(&("which ".to_owned() + &" ".repeat(10_000) + "first")),
    ] {
        all_options(&corpus, &q, &now);
    }
    let hits = corpus
        .search(&unicode::cps("docker"), &now, &Options::default())
        .unwrap();
    assert!(!hits.is_empty());
}

#[test]
fn extreme_arguments() {
    let now = utc(2026, 6, 1);
    let corpus = Corpus::new(&[record(0, "docker", now), record(1, "docker x", now)]).unwrap();
    let q = unicode::cps("docker");
    for half_life in [
        0.0,
        -1.0,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::MIN_POSITIVE,
        1e308,
    ] {
        for mode in MODES {
            let opts = Options {
                mode,
                half_life_days: half_life,
                ..Options::default()
            };
            assert!(corpus.search(&q, &now, &opts).is_ok());
        }
    }
    let opts = Options {
        half_life_days: f64::NAN,
        ..Options::default()
    };
    assert_eq!(
        corpus.search(&q, &now, &opts),
        Err(SearchError::InvalidInput)
    );
    for k in [i64::MIN, -1, 0] {
        let opts = Options {
            rrf_k: k,
            ..Options::default()
        };
        assert_eq!(corpus.search(&q, &now, &opts), Err(SearchError::ValueError));
    }
    let opts = Options {
        rrf_k: i64::MAX,
        ..Options::default()
    };
    assert_eq!(corpus.search(&q, &now, &opts).unwrap().len(), 2);
    assert_eq!(Mode::parse("semantic"), Err(SearchError::ValueError));
    // The convenience entry point.
    assert!(search(&[], &q, &now, &Options::default())
        .unwrap()
        .is_empty());
}

#[test]
fn absurd_corpus_statistics() {
    let now = utc(2026, 6, 1);
    let corpus = Corpus::new(&[
        record(0, "docker compose", now),
        record(1, "docker", now),
        record(2, "something else entirely", now),
    ])
    .unwrap();
    let q = unicode::cps("docker compose last week toggles");
    let stats = [
        (i64::MAX, i64::MAX, Some(f64::NAN)),
        (i64::MAX, 1, Some(f64::INFINITY)),
        (1, i64::MAX, Some(-1.0)),
        (3, 1_000, None),
        (-5, 2, Some(0.0)),
        (i64::MIN, i64::MIN, Some(f64::MIN_POSITIVE)),
        (0, 0, None),
    ];
    for (size, df, avgdl) in stats {
        let provider = move |terms: &[String]| {
            Some(CorpusStats {
                size,
                body_df: terms.iter().map(|t| (t.clone(), df)).collect(),
                scope_df: terms.iter().map(|t| (t.clone(), df)).collect(),
                avgdl,
            })
        };
        for mode in [Mode::Bm25, Mode::Hybrid] {
            for rescue in [false, true] {
                let opts = Options {
                    mode,
                    rescue_expansion: rescue,
                    corpus_stats_provider: Some(&provider),
                    ..Options::default()
                };
                match corpus.search(&q, &now, &opts) {
                    Ok(_) | Err(SearchError::ValueError) => {}
                    Err(e) => panic!("{e:?} for {size} {df} {avgdl:?}"),
                }
            }
        }
    }
    // A df beyond the collection's size prices the term below zero (the
    // log's argument stays positive for any positive size): its hits drop.
    let provider = |terms: &[String]| {
        Some(CorpusStats {
            size: 3,
            body_df: terms.iter().map(|t| (t.clone(), 1_000)).collect(),
            ..CorpusStats::default()
        })
    };
    let opts = Options {
        mode: Mode::Bm25,
        corpus_stats_provider: Some(&provider),
        ..Options::default()
    };
    assert!(corpus.search(&q, &now, &opts).is_ok());
    let none = |_: &[String]| None;
    let opts = Options {
        corpus_stats_provider: Some(&none),
        ..Options::default()
    };
    assert!(!corpus.search(&q, &now, &opts).unwrap().is_empty());
}

#[test]
fn duplicate_ids() {
    let now = utc(2026, 6, 1);
    let records = vec![
        record(0, "docker", now),
        record(0, "docker docker", now),
        record(0, "docker", now),
    ];
    let corpus = Corpus::new(&records).unwrap();
    for mode in MODES {
        let opts = Options {
            mode,
            ..Options::default()
        };
        let hits = corpus.search(&unicode::cps("docker"), &now, &opts).unwrap();
        assert!(!hits.is_empty());
    }
}
