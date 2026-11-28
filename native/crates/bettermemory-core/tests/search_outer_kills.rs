//! Inputs on which a plausible wrong `search::outer` answers otherwise
//! than the reference: a writer filter that admits a record with no actor
//! or folds case, the homes primed for an empty query, twice or out of
//! order, a record with no home admitted, the filters applied after the
//! ranking (so the BM25 denominator is the whole list's), the browse
//! sorted on another key, a leg recorded for a browse hit, expansion
//! reported for a hit a base leg scored, a legs dict losing an id's place,
//! `hydrate` asked for an empty page or its length passed over on the
//! ranked path, a browse over naive and aware stamps answered otherwise
//! than TypeError, `rank` filtering by scope, trimming, running the rescue
//! leg or browsing a stopword-only query, and an anchor's
//! translation not asked, or a path's code points lost, on its way to the
//! drift read.
//!
//! The memories are `search_outer.jsonl`'s (`gen_search_hit.py`): 106
//! "The removal of the old cache went fine." (a, b; claude-code/opus),
//! 107 "Rip the flag out of the parser body." (b; Claude-Code), 108 "A
//! toggle to rip out and flag later." (c; no actor), 109 (a, c;
//! cursor/opus) and 110 (d; model opus) updated at one instant, 111
//! "Cache removal toggle notes." (a; claude-code/Opus) updated at 106's
//! instant spelled at +02:00.

mod common;

use std::cell::RefCell;
use std::rc::Rc;

use bettermemory_core::attestation_paths::Paths;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::records::summaries::MemoryHit;
use bettermemory_core::records::Memory;
use bettermemory_core::search::hit::{Drift, RecordAnchors};
use bettermemory_core::search::outer::{
    self, candidate_admitted, Admission, EffectError, Effects, LegsOut, OuterError, Selection,
    LEG_EXPANSION, LEG_LEXICAL,
};
use bettermemory_core::search::snippet::SnippetMemo;
use bettermemory_core::search::{Mode, Options, SearchError};
use bettermemory_core::unicode::{self, cps};
use bettermemory_core::verify_anchoring::anchor::{AnchorPaths, ClaimAnchor};
use bettermemory_core::verify_anchoring::detect::DriftError;
use bettermemory_core::verify_anchoring::passes::ReadAnchor;
use bettermemory_core::verify_anchoring::report::PathDriftReport;
use common::search_hit::memory;

/// The header's pool (every memory but the naive one) and `now`.
fn memories() -> (Vec<Memory>, DateTime) {
    let lines = common::jsonl("search_outer.jsonl");
    let pool = lines[0]["pool"].as_u64().unwrap() as usize;
    let ms = lines[0]["memories"]
        .as_array()
        .expect("memories")
        .iter()
        .take(pool)
        .map(memory)
        .collect();
    let now = DateTime::from_isoformat(&cps(lines[0]["now"].as_str().unwrap())).unwrap();
    (ms, now)
}

fn id(n: u32) -> String {
    format!("01J{n:023}")
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

/// An admission that logs its calls and admits every record with a home;
/// `homes` names the homed ids.
struct Logged {
    homes: Vec<String>,
    log: Vec<String>,
}

impl Logged {
    fn new(homes: &[u32]) -> Logged {
        Logged {
            homes: homes.iter().map(|&n| id(n)).collect(),
            log: Vec::new(),
        }
    }
}

impl Admission for Logged {
    fn prime(&mut self, record_ids: &[String]) -> Result<(), EffectError> {
        self.log.push(format!("prime {}", record_ids.join(",")));
        Ok(())
    }
    fn home(&mut self, record_id: &str) -> Result<Option<PyStr>, EffectError> {
        self.log.push(format!("home {record_id}"));
        Ok(self.homes.iter().any(|h| h == record_id).then(|| cps("p")))
    }
    fn admits_row(&mut self, _: &[PyStr], recorded: Option<&[u32]>) -> Result<bool, EffectError> {
        self.log.push(format!("admits {}", recorded.is_some()));
        Ok(recorded.is_some())
    }
}

fn opts(mode: Mode) -> Options<'static> {
    Options {
        mode,
        ..Options::default()
    }
}

/// `search` with quiet effects; `hydrate` from `hy` when given.
fn run(
    ms: &[Memory],
    q: &str,
    now: &DateTime,
    opts: &Options<'_>,
    sel: &mut Selection<'_>,
    hy: Option<outer::Hydrate<'_>>,
    legs: Option<&mut LegsOut>,
) -> Result<Vec<MemoryHit>, OuterError> {
    let memo = SnippetMemo::default();
    let mut drift = Quiet;
    let mut fx = Effects {
        anchoring: None,
        paths: &NoPaths,
        drift: &mut drift,
        memo: &memo,
        hydrate: hy.map(|h| -> outer::Hydrate<'_> { h }),
        path_drift: false,
    };
    outer::search(ms, &cps(q), now, opts, sel, &mut fx, legs)
}

fn ids(hits: &[MemoryHit]) -> Vec<String> {
    hits.iter()
        .map(|h| unicode::to_string(&h.id).unwrap())
        .collect()
}

#[test]
fn a_record_with_no_actor_matches_no_writer_filter() {
    let (ms, now) = memories();
    let empty = Default::default();
    let client = cps("claude-code");
    for (c, m) in [(Some(&client[..]), None), (None, Some(&client[..]))] {
        let mut sel = Selection {
            client: c,
            model: m,
            ..Selection::default()
        };
        let got = candidate_admitted(&ms[8].fields.scopes, None, None, None, &empty, &mut sel);
        assert_eq!(got, Ok(false));
    }
    let mut none = Selection::default();
    assert_eq!(
        candidate_admitted(&ms[8].fields.scopes, None, None, None, &empty, &mut none),
        Ok(true)
    );
    let mut sel = Selection {
        client: Some(&client),
        ..Selection::default()
    };
    // 108 holds "toggle" and has no actor.
    let hits = run(
        &ms,
        "removal toggle",
        &now,
        &opts(Mode::Hybrid),
        &mut sel,
        None,
        None,
    )
    .unwrap();
    assert_eq!(ids(&hits), [id(111), id(106)]);
}

#[test]
fn the_writer_filters_are_exact_and_case_sensitive() {
    let (ms, now) = memories();
    let (client, model) = (cps("claude-code"), cps("opus"));
    let mut sel = Selection {
        client: Some(&client),
        ..Selection::default()
    };
    let hits = run(
        &ms,
        "flag removal",
        &now,
        &opts(Mode::Keyword),
        &mut sel,
        None,
        None,
    )
    .unwrap();
    assert!(
        !ids(&hits).contains(&id(107)),
        "Claude-Code is another client"
    );
    let mut sel = Selection {
        model: Some(&model),
        ..Selection::default()
    };
    let hits = run(
        &ms,
        "removal toggle",
        &now,
        &opts(Mode::Keyword),
        &mut sel,
        None,
        None,
    )
    .unwrap();
    assert_eq!(ids(&hits), [id(109), id(106)], "Opus is another model");
}

#[test]
fn the_homes_are_primed_once_in_order_and_never_for_an_empty_query() {
    let (ms, now) = memories();
    let mut adm = Logged::new(&[106]);
    let mut sel = Selection {
        admission: Some(&mut adm),
        ..Selection::default()
    };
    assert_eq!(
        run(&ms, "", &now, &opts(Mode::Hybrid), &mut sel, None, None),
        Ok(vec![])
    );
    assert!(adm.log.is_empty(), "{:?}", adm.log);
    assert_eq!(
        outer::rank(&ms, &cps(""), &now, &opts(Mode::Hybrid), Some(&mut adm)),
        Ok(vec![])
    );
    assert!(adm.log.is_empty(), "{:?}", adm.log);
    let mut sel = Selection {
        admission: Some(&mut adm),
        ..Selection::default()
    };
    let hits = run(
        &ms,
        "removal",
        &now,
        &opts(Mode::Hybrid),
        &mut sel,
        None,
        None,
    )
    .unwrap();
    assert_eq!(ids(&hits), [id(106)], "only a homed record is admitted");
    let all: Vec<String> = (100..112).map(id).collect();
    assert_eq!(adm.log[0], format!("prime {}", all.join(",")));
    assert_eq!(adm.log.iter().filter(|l| l.starts_with("prime")).count(), 1);
    let homes: Vec<&String> = adm.log.iter().filter(|l| l.starts_with("home")).collect();
    assert_eq!(homes.len(), 12);
    for (line, want) in homes.iter().zip(&all) {
        assert_eq!(**line, format!("home {want}"));
    }
    assert!(
        adm.log.contains(&"admits false".to_string()),
        "a record with no home is asked"
    );
}

#[test]
fn the_filters_come_before_the_ranking() {
    let (ms, now) = memories();
    let excluded = ["a".to_string()];
    let o = Options {
        excluded_scopes: Some(&excluded),
        ..opts(Mode::Bm25)
    };
    let mut sel = Selection::default();
    let hits = run(&ms, "removal toggle", &now, &o, &mut sel, None, None).unwrap();
    assert_eq!(ids(&hits), [id(108)]);
    // Over the candidates the score is 1.0803; ranked over every record
    // and filtered after, 2.1682.
    assert_eq!(hits[0].score, 1.0803);
    let alone: Vec<Memory> = ms
        .iter()
        .filter(|m| !m.fields.scopes.contains(&cps("a")))
        .cloned()
        .collect();
    let again = run(
        &alone,
        "removal toggle",
        &now,
        &opts(Mode::Bm25),
        &mut sel,
        None,
        None,
    )
    .unwrap();
    assert_eq!(again[0].score.to_bits(), hits[0].score.to_bits());
    let whole = run(
        &ms,
        "removal toggle",
        &now,
        &opts(Mode::Bm25),
        &mut sel,
        None,
        None,
    )
    .unwrap();
    let all_108 = whole.iter().find(|h| h.id == cps(&id(108))).unwrap();
    assert_eq!(all_108.score, 2.1682);
}

#[test]
fn the_browse_is_newest_updated_then_greatest_id() {
    let (ms, now) = memories();
    let o = Options {
        allow_empty_query: true,
        max_results: 20,
        ..Options::default()
    };
    let mut sel = Selection::default();
    let hits = run(&ms, "", &now, &o, &mut sel, None, None).unwrap();
    let want: Vec<String> = [110, 109, 108, 107, 111, 106, 105, 104, 103, 102, 101, 100]
        .into_iter()
        .map(id)
        .collect();
    assert_eq!(ids(&hits), want);
    assert!(hits
        .iter()
        .all(|h| h.score == 0.0 && h.match_terms.is_empty()));
}

#[test]
fn a_browse_over_naive_and_aware_updated_is_type_error() {
    let (ms, now) = memories();
    let lines = common::jsonl("search_outer.jsonl");
    let naive = memory(lines[0]["memories"].as_array().unwrap().last().unwrap());
    assert_eq!(naive.fields.updated.offset_us, None);
    let o = Options {
        allow_empty_query: true,
        ..Options::default()
    };
    let mut sel = Selection::default();
    let mixed = [naive.clone(), ms[1].clone()];
    let got = run(&mixed, "", &now, &o, &mut sel, None, None);
    assert_eq!(got, Err(OuterError::Search(SearchError::TypeError)));
    let alone = run(&[naive], "", &now, &o, &mut sel, None, None).unwrap();
    assert_eq!(alone.len(), 1);
}

#[test]
fn a_browse_hit_has_no_leg() {
    let (ms, now) = memories();
    let o = Options {
        allow_empty_query: true,
        ..Options::default()
    };
    let mut sel = Selection::default();
    let mut legs: LegsOut = vec![(id(110), "kept".into())];
    let hits = run(&ms, "the", &now, &o, &mut sel, None, Some(&mut legs)).unwrap();
    assert_eq!(hits.len(), 5);
    assert_eq!(legs, [(id(110), "kept".to_string())]);
}

fn rescue() -> Options<'static> {
    Options {
        rescue_expansion: true,
        ..Options::default()
    }
}

#[test]
fn lexical_wins_and_expansion_only_names_what_no_base_leg_scored() {
    let (ms, now) = memories();
    let bc = ["b".to_string(), "c".to_string()];
    let o = Options {
        scopes: Some(&bc),
        ..rescue()
    };
    let mut sel = Selection::default();
    let mut legs: LegsOut = vec![(id(107), "stale".into()), (id(105), "x".into())];
    let hits = run(
        &ms,
        "removal toggle",
        &now,
        &o,
        &mut sel,
        None,
        Some(&mut legs),
    )
    .unwrap();
    assert_eq!(ids(&hits), [id(108), id(109), id(106), id(107)]);
    // 108 holds "toggle" and the expansion terms "rip" and "flag".
    assert_eq!(
        legs,
        [
            (id(107), LEG_EXPANSION.to_string()),
            (id(105), "x".to_string()),
            (id(108), LEG_LEXICAL.to_string()),
            (id(109), LEG_LEXICAL.to_string()),
            (id(106), LEG_LEXICAL.to_string()),
        ]
    );
    // Trimmed to one, only that hit's leg is set.
    let o = Options {
        max_results: 1,
        ..o
    };
    let mut legs = LegsOut::new();
    run(
        &ms,
        "removal toggle",
        &now,
        &o,
        &mut sel,
        None,
        Some(&mut legs),
    )
    .unwrap();
    assert_eq!(legs, [(id(108), LEG_LEXICAL.to_string())]);
}

#[test]
fn the_ranked_path_refuses_a_hydrate_of_another_length_and_the_browse_keeps_it() {
    let (ms, now) = memories();
    let mut sel = Selection::default();
    let calls = RefCell::new(0);
    for cut in [1usize, 0] {
        let mut shorter = |rs: &[Memory]| -> Result<Vec<Memory>, EffectError> {
            *calls.borrow_mut() += 1;
            let mut v = rs.to_vec();
            if cut == 1 {
                v.pop();
            } else {
                v.push(rs[0].clone());
            }
            Ok(v)
        };
        let mut legs = LegsOut::new();
        let got = run(
            &ms,
            "body origin",
            &now,
            &opts(Mode::Hybrid),
            &mut sel,
            Some(&mut shorter),
            Some(&mut legs),
        );
        assert_eq!(got, Err(OuterError::Search(SearchError::ValueError)));
        assert_eq!(legs.len(), 5, "the legs are set before hydrate");
        let browse = Options {
            allow_empty_query: true,
            ..Options::default()
        };
        let got = run(&ms, "", &now, &browse, &mut sel, Some(&mut shorter), None).unwrap();
        assert_eq!(got.len(), if cut == 1 { 4 } else { 6 });
    }
    assert_eq!(*calls.borrow(), 4);
}

#[test]
fn hydrate_is_never_asked_for_an_empty_page() {
    let (ms, now) = memories();
    let mut sel = Selection::default();
    let calls = RefCell::new(0);
    let mut count = |rs: &[Memory]| -> Result<Vec<Memory>, EffectError> {
        *calls.borrow_mut() += 1;
        Ok(rs.to_vec())
    };
    let none = Options {
        max_results: 0,
        ..Options::default()
    };
    let browse = Options {
        allow_empty_query: true,
        ..none
    };
    for (q, o) in [
        ("removal", &none),
        ("", &browse),
        ("zebra quokka", &opts(Mode::Hybrid)),
    ] {
        assert_eq!(
            run(&ms, q, &now, o, &mut sel, Some(&mut count), None),
            Ok(vec![])
        );
    }
    assert_eq!(*calls.borrow(), 0);
}

#[test]
fn the_hits_are_built_from_the_hydrated_records() {
    let (ms, now) = memories();
    let mut sel = Selection::default();
    let mut swap = |rs: &[Memory]| -> Result<Vec<Memory>, EffectError> {
        Ok(rs.iter().map(|_| ms[8].clone()).collect())
    };
    let mut legs = LegsOut::new();
    let o = Options {
        max_results: 2,
        ..Options::default()
    };
    let hits = run(
        &ms,
        "removal",
        &now,
        &o,
        &mut sel,
        Some(&mut swap),
        Some(&mut legs),
    )
    .unwrap();
    assert_eq!(ids(&hits), [id(108), id(108)]);
    assert_eq!(hits[0].snippet, ms[8].fields.body);
    assert_eq!(hits[0].match_terms, [cps("removal")]);
    assert!(
        legs.iter().all(|(k, _)| *k != id(108)),
        "the legs name the ranked ids"
    );
}

#[test]
fn rank_filters_by_no_scope_and_trims_nothing() {
    let (ms, now) = memories();
    let only_c = ["c".to_string()];
    let o = Options {
        scopes: Some(&only_c),
        max_results: 1,
        rescue_expansion: true,
        ..opts(Mode::Keyword)
    };
    let got = outer::rank(&ms, &cps("removal toggle"), &now, &o, None).unwrap();
    let got_ids: Vec<&str> = got.iter().map(|(i, _)| i.as_str()).collect();
    assert_eq!(got_ids, [id(111), id(109), id(108), id(106)]);
    assert_eq!(got[0].1, ["removal", "toggl"]);
    let excluded = ["c".to_string()];
    let o = Options {
        excluded_scopes: Some(&excluded),
        ..o
    };
    let got = outer::rank(&ms, &cps("removal toggle"), &now, &o, None).unwrap();
    let got_ids: Vec<&str> = got.iter().map(|(i, _)| i.as_str()).collect();
    assert_eq!(got_ids, [id(111), id(106)]);
}

/// `rank` passes the reference's `rescue_expansion=False` and
/// `_query_tokens(query, False)` whatever `Options` holds: in BM25 and in
/// hybrid mode, where the rescue leg runs, the leg adds no hit, and a
/// stopword-only query is ranked on its own tokens (the fallback), never
/// browsed.
#[test]
fn rank_runs_no_rescue_leg_and_never_browses() {
    let (ms, now) = memories();
    let excluded = ["a".to_string()];
    let filtered = Options {
        excluded_scopes: Some(&excluded),
        ..rescue()
    };
    // The search the same options give: the rescue leg surfaces 107,
    // which holds no query token.
    let mut sel = Selection::default();
    let hits = run(&ms, "removal toggle", &now, &filtered, &mut sel, None, None).unwrap();
    assert_eq!(ids(&hits), [id(108), id(107)]);
    let the = |ns: [u32; 5]| -> Vec<(String, Vec<String>)> {
        ns.into_iter()
            .map(|n| (id(n), vec!["the".into()]))
            .collect()
    };
    for (mode, stopword) in [
        (Mode::Bm25, the([107, 106, 104, 110, 109])),
        (Mode::Hybrid, the([107, 104, 106, 110, 109])),
    ] {
        let asked = Options {
            mode,
            allow_empty_query: true,
            ..filtered
        };
        let got = outer::rank(&ms, &cps("removal toggle"), &now, &asked, None).unwrap();
        assert_eq!(got, [(id(108), vec!["toggl".to_string()])], "{mode:?}");
        let asked = Options {
            excluded_scopes: None,
            ..asked
        };
        let got = outer::rank(&ms, &cps("the"), &now, &asked, None).unwrap();
        assert_eq!(got, stopword, "{mode:?}");
    }
}

/// The drift read asks the anchor for three paths (one with a lone
/// surrogate) and its root and rule.
struct Asks(Vec<(Option<PyStr>, String)>);

impl Drift for Asks {
    fn detect(
        &mut self,
        _: &[u32],
        _: &Paths,
        _: &Paths,
        _: Option<&[u32]>,
        anchor: Option<&dyn ReadAnchor>,
    ) -> Result<PathDriftReport, DriftError> {
        let a = anchor.expect("an anchor");
        for p in [cps("src/a.py"), cps("gone"), vec![0x61, 0xD800]] {
            self.0.push(a.absolute(&p));
        }
        self.0.push((a.root(), a.rule()));
        self.0.push((None, a.convicting().to_string()));
        Ok(PathDriftReport::default())
    }
}

/// Every record anchored at `/live`, translating when `translate` is set.
struct Live {
    translate: bool,
}

impl RecordAnchors for Live {
    fn prime(&mut self, _: &[String]) {}
    fn for_record(&mut self, _: &str, _: Option<&[u32]>) -> Option<ClaimAnchor> {
        let mut a = ClaimAnchor::new(Some("/live".into()), "home_checkout");
        if self.translate {
            a.translate = Some(Rc::new(|p: &str| {
                if p == "gone" {
                    (String::new(), "unanchored".into())
                } else {
                    (format!("/live/{p}"), "home_checkout".into())
                }
            }));
        }
        Some(a)
    }
}

#[test]
fn the_anchor_reaches_the_drift_read_with_its_translation_and_code_points() {
    let (ms, now) = memories();
    let o = Options {
        max_results: 1,
        ..Options::default()
    };
    let surrogate = vec![0x61, 0xD800];
    for translate in [false, true] {
        let memo = SnippetMemo::default();
        let mut asks = Asks(Vec::new());
        let mut live = Live { translate };
        let mut fx = Effects {
            anchoring: Some(&mut live),
            paths: &NoPaths,
            drift: &mut asks,
            memo: &memo,
            hydrate: None,
            path_drift: true,
        };
        let mut sel = Selection::default();
        let hits = outer::search(&ms, &cps("removal"), &now, &o, &mut sel, &mut fx, None).unwrap();
        assert_eq!(hits.len(), 1);
        let rule = |r: &str| r.to_string();
        let want = if translate {
            vec![
                (Some(cps("/live/src/a.py")), rule("home_checkout")),
                (None, rule("unanchored")),
                (None, rule("unanchored")),
            ]
        } else {
            vec![
                (Some(cps("src/a.py")), rule("absolute")),
                (Some(cps("gone")), rule("absolute")),
                (Some(surrogate.clone()), rule("absolute")),
            ]
        };
        assert_eq!(asks.0[..3], want[..], "translate {translate}");
        assert_eq!(asks.0[3], (Some(cps("/live")), rule("home_checkout")));
        assert_eq!(asks.0[4], (None, rule("true")), "home_checkout convicts");
    }
}
