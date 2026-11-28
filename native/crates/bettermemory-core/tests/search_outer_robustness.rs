//! Hostile input for `search::outer`: seeded random corpora, filters,
//! modes, admissions, legs dicts and `hydrate` lengths (no panic, and the
//! invariants the reference keeps), `max_results` at the integer bounds,
//! and the filter and the legs record growing linearly.

mod common;
mod timing;

use std::collections::HashSet;
use std::hint::black_box;

use bettermemory_core::attestation_paths::Paths;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::records::{Actor, Memory};
use bettermemory_core::search::hit::Drift;
use bettermemory_core::search::outer::{
    self, filter_candidates, record_matched_legs, Admission, EffectError, Effects, LegsOut,
    OuterError, Selection,
};
use bettermemory_core::search::snippet::SnippetMemo;
use bettermemory_core::search::{Legs, Mode, Options, SearchError};
use bettermemory_core::unicode::{self, cps};
use bettermemory_core::verify_anchoring::anchor::AnchorPaths;
use bettermemory_core::verify_anchoring::detect::DriftError;
use bettermemory_core::verify_anchoring::passes::ReadAnchor;
use bettermemory_core::verify_anchoring::report::PathDriftReport;
use common::search_hit::memory;

struct Lcg(u64);

impl Lcg {
    fn next(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 33
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

struct Quiet;

impl Drift for Quiet {
    fn detect(
        &mut self,
        _: &[u32],
        _: &Paths,
        _: &Paths,
        _: Option<&[u32]>,
        _: Option<&dyn ReadAnchor>,
    ) -> Result<PathDriftReport, DriftError> {
        Ok(PathDriftReport::default())
    }
}

struct NoPaths;

impl AnchorPaths for NoPaths {
    fn expected_home(&self, _: &str, _: &[PyStr]) -> bool {
        false
    }
    fn relative_under(&self, _: &str, _: Option<&str>) -> Option<String> {
        None
    }
    fn inside(&self, _: &str, _: &str) -> bool {
        false
    }
}

/// Homes by the id's last digit; admits a row whose home is even.
struct Parity {
    primed: usize,
}

impl Admission for Parity {
    fn prime(&mut self, _: &[String]) -> Result<(), EffectError> {
        self.primed += 1;
        Ok(())
    }
    fn home(&mut self, record_id: &str) -> Result<Option<PyStr>, EffectError> {
        let d = record_id.bytes().last().unwrap_or(b'0') - b'0';
        Ok((!d.is_multiple_of(3)).then(|| vec![u32::from(d)]))
    }
    fn admits_row(&mut self, _: &[PyStr], recorded: Option<&[u32]>) -> Result<bool, EffectError> {
        Ok(recorded.is_some_and(|r| r[0].is_multiple_of(2)))
    }
}

const WORDS: [&str; 12] = [
    "removal",
    "toggle",
    "flag",
    "rip",
    "cache",
    "deploy",
    "the",
    "config",
    "old",
    "body",
    "staging-config",
    "notes",
];

fn template() -> (Memory, DateTime) {
    let lines = common::jsonl("search_outer.jsonl");
    let m = memory(&lines[0]["memories"][6]);
    let now = DateTime::from_isoformat(&cps(lines[0]["now"].as_str().unwrap())).unwrap();
    (m, now)
}

fn corpus(rng: &mut Lcg, n: usize, base: &Memory) -> Vec<Memory> {
    (0..n)
        .map(|i| {
            let mut m = base.clone();
            m.fields.id = cps(&format!(
                "01J{:023}",
                rng.below(n as u64 * 2) + i as u64 % 3
            ));
            let words: Vec<&str> = (0..1 + rng.below(12))
                .map(|_| WORDS[rng.below(12) as usize])
                .collect();
            m.fields.body = cps(&words.join(" "));
            m.fields.scopes = (0..1 + rng.below(3))
                .map(|_| cps(["a", "b", "c", "d"][rng.below(4) as usize]))
                .collect();
            m.fields.actor = match rng.below(4) {
                0 => None,
                1 => Some(Actor {
                    client: Some(cps("x")),
                    ..actor()
                }),
                2 => Some(Actor {
                    model: Some(cps("m")),
                    ..actor()
                }),
                _ => Some(actor()),
            };
            m.fields.updated = m
                .fields
                .updated
                .minus_micros(i128::from(rng.below(5)) * 3_600_000_000)
                .unwrap();
            m
        })
        .collect()
}

fn actor() -> Actor {
    Actor {
        client: None,
        client_version: None,
        model: None,
        principal: None,
        session: None,
        sources: Vec::new(),
    }
}

#[test]
fn random_searches_keep_the_reference_invariants() {
    let (base, now) = template();
    let mut rng = Lcg(0x5eed_0e7e);
    let scopes = ["a".to_string(), "c".to_string()];
    let (x, m) = (cps("x"), cps("m"));
    for round in 0..400 {
        let n = 1 + rng.below(40) as usize;
        let ms = corpus(&mut rng, n, &base);
        let q: Vec<&str> = (0..rng.below(4))
            .map(|_| WORDS[rng.below(12) as usize])
            .collect();
        let query = cps(&q.join(" "));
        let mode = [Mode::Keyword, Mode::Bm25, Mode::Hybrid][rng.below(3) as usize];
        let opts = Options {
            mode,
            max_results: [0, 1, 5, -1, -50, i64::MAX, i64::MIN][rng.below(7) as usize],
            rescue_expansion: rng.below(2) == 0,
            allow_empty_query: rng.below(2) == 0,
            scopes: (rng.below(3) == 0).then_some(&scopes[..1]),
            excluded_scopes: (rng.below(3) == 0).then_some(&scopes[1..]),
            ..Options::default()
        };
        let mut adm = Parity { primed: 0 };
        let mut sel = Selection {
            admission: (rng.below(2) == 0).then_some(&mut adm as &mut dyn Admission),
            client: (rng.below(4) == 0).then_some(&x[..]),
            model: (rng.below(4) == 0).then_some(&m[..]),
        };
        let delta = rng.below(3) as i64 - 1;
        let mut hydrate = |rs: &[Memory]| -> Result<Vec<Memory>, EffectError> {
            let mut v = rs.to_vec();
            match delta {
                -1 => {
                    v.pop();
                }
                1 => v.push(rs[0].clone()),
                _ => {}
            }
            Ok(v)
        };
        let memo = SnippetMemo::default();
        let mut drift = Quiet;
        let mut fx = Effects {
            anchoring: None,
            paths: &NoPaths,
            drift: &mut drift,
            memo: &memo,
            hydrate: Some(&mut hydrate),
            path_drift: false,
        };
        let mut legs = LegsOut::new();
        let got = outer::search(&ms, &query, &now, &opts, &mut sel, &mut fx, Some(&mut legs));
        let (tokens, _) = bettermemory_core::search::query_tokens(&query, opts.allow_empty_query);
        let browsing = tokens.is_empty();
        match got {
            Ok(hits) => {
                if browsing {
                    assert!(legs.is_empty(), "round {round}: a browse records no leg");
                    assert!(hits
                        .iter()
                        .all(|h| h.score == 0.0 && h.match_terms.is_empty()));
                } else {
                    assert!(delta == 0 || hits.is_empty(), "round {round}");
                    let hit_ids: HashSet<String> = hits
                        .iter()
                        .map(|h| unicode::to_string(&h.id).unwrap())
                        .collect();
                    assert!(
                        legs.iter().all(|(k, _)| hit_ids.contains(k)),
                        "round {round}"
                    );
                }
            }
            Err(e) => {
                assert_eq!(
                    e,
                    OuterError::Search(SearchError::ValueError),
                    "round {round}"
                );
                assert!(!browsing && delta != 0, "round {round}");
            }
        }
        let primed = adm.primed;
        let asked = sel_admission_used(&opts, &query);
        assert!(
            primed <= usize::from(asked),
            "round {round}: primed {primed}"
        );
        let ranked = outer::rank(&ms, &query, &now, &opts, None);
        assert!(ranked.is_ok(), "round {round}");
    }
}

/// Whether `search` reads the admission at all: not for an empty query
/// that may not browse.
fn sel_admission_used(opts: &Options<'_>, query: &[u32]) -> bool {
    let (tokens, _) = bettermemory_core::search::query_tokens(query, opts.allow_empty_query);
    !tokens.is_empty() || opts.allow_empty_query
}

#[test]
fn max_results_at_the_integer_bounds() {
    let (base, now) = template();
    let mut rng = Lcg(7);
    let ms = corpus(&mut rng, 30, &base);
    for max_results in [i64::MIN, i64::MIN + 1, -1, 0, 1, i64::MAX - 1, i64::MAX] {
        for allow in [false, true] {
            let opts = Options {
                max_results,
                allow_empty_query: allow,
                ..Options::default()
            };
            let memo = SnippetMemo::default();
            let mut drift = Quiet;
            let mut fx = Effects {
                anchoring: None,
                paths: &NoPaths,
                drift: &mut drift,
                memo: &memo,
                hydrate: None,
                path_drift: false,
            };
            let q = if allow { "" } else { "removal toggle" };
            let got = outer::search(
                &ms,
                &cps(q),
                &now,
                &opts,
                &mut Selection::default(),
                &mut fx,
                None,
            )
            .unwrap();
            assert!(got.len() <= 30);
        }
    }
}

#[test]
fn the_filter_grows_linearly() {
    let (base, _) = template();
    let mut rng = Lcg(11);
    let small = corpus(&mut rng, 2_000, &base);
    let large = corpus(&mut rng, 20_000, &base);
    let excluded = ["c".to_string()];
    let x = cps("x");
    let time = |ms: &[Memory]| {
        timing::once(|| {
            let mut adm = Parity { primed: 0 };
            let mut sel = Selection {
                admission: Some(&mut adm),
                client: Some(&x),
                model: None,
            };
            black_box(
                filter_candidates(ms, None, Some(&excluded), &mut sel)
                    .unwrap()
                    .len(),
            )
        })
    };
    timing::grows_within(
        "filter_candidates",
        timing::TENFOLD,
        &mut || time(&small),
        &mut || time(&large),
    );
}

#[test]
fn the_legs_record_grows_linearly() {
    let ids = |n: usize| -> Vec<String> { (0..n).map(|i| format!("01J{i:023}")).collect() };
    let (small, large) = (ids(10_000), ids(100_000));
    let legs_of = |v: &[String]| Legs {
        lexical: v.iter().step_by(2).cloned().collect(),
        expansion: v.iter().cloned().collect(),
    };
    let (ls, ll) = (legs_of(&small), legs_of(&large));
    let out_of =
        |v: &[String]| -> LegsOut { v.iter().rev().map(|k| (k.clone(), "x".into())).collect() };
    let (os, ol) = (out_of(&small), out_of(&large));
    let run = |v: &[String], legs: &Legs, out: &LegsOut| {
        let mut out = out.clone();
        let trimmed: Vec<&str> = v.iter().map(String::as_str).collect();
        timing::once(|| {
            record_matched_legs(&trimmed, legs, &mut out);
            black_box(out.len())
        })
    };
    timing::grows_within(
        "record_matched_legs",
        timing::TENFOLD,
        &mut || run(&small, &ls, &os),
        &mut || run(&large, &ll, &ol),
    );
    let mut out = os.clone();
    let trimmed: Vec<&str> = small.iter().map(String::as_str).collect();
    record_matched_legs(&trimmed, &ls, &mut out);
    assert_eq!(out.len(), small.len());
    assert_eq!(out[0].0, small[small.len() - 1]);
    assert!(out.iter().all(|(_, v)| v == "lexical" || v == "expansion"));
}
