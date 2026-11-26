//! Inputs on which a plausible wrong `search::hit` answers otherwise than
//! the reference: the drift read made when the caller turned it off, the
//! origin's root passed beside an anchor, another bucket on a hit field,
//! the score rounded half away from zero, the relevance bounds moved, the
//! snippet budget moved, and `_claim_anchors` priming per record or
//! reading an empty origin root as none.

mod common;

use bettermemory_core::records::Memory;
use bettermemory_core::search::hit::{build_hit, claim_anchors, Drift, RecordAnchors, Scored};
use bettermemory_core::search::snippet::SnippetMemo;
use bettermemory_core::verify_anchoring::detect::DriftError;
use bettermemory_core::verify_candidates::scan::ScanError;
use common::search_hit::{anchor_desc, hit, memory, report, Anchor, Logged, Table};
use serde_json::{json, Value};

fn memories() -> Vec<Memory> {
    let lines = common::jsonl("search_hit.jsonl");
    lines[0]["memories"]
        .as_array()
        .expect("memories")
        .iter()
        .map(memory)
        .collect()
}

fn full() -> Value {
    json!({
        "checked": ["c1", "c2"], "missing": ["m1"], "verified": ["v1"],
        "expected_absent": ["e1"], "dropped_as_route": ["d1"],
        "claim_anchored_missing": ["k1"], "present_but_attested_absent": ["p1"],
        "unconfirmed": ["u1"], "unconfirmed_reasons": ["r1"], "anchor": "x",
    })
}

fn run(
    memory: &Memory,
    score: f64,
    matched: &[&str],
    qu: usize,
    anchor: Option<&Anchor>,
    path_drift: bool,
) -> (Value, Vec<Value>) {
    let matched: Vec<String> = matched.iter().map(|s| (*s).to_string()).collect();
    let mut drift = Logged {
        report: report(&full()),
        log: Vec::new(),
    };
    let scored = Scored {
        memory,
        score,
        matched: &matched,
        query_unique: qu,
    };
    let read = anchor.map(|a| a as _);
    let got = build_hit(
        &SnippetMemo::default(),
        &mut drift,
        &scored,
        read,
        path_drift,
    )
    .unwrap();
    (hit(&got), drift.log)
}

fn field(h: &Value, name: &str) -> Value {
    h.as_array()
        .unwrap()
        .iter()
        .find(|p| p[0] == name)
        .map(|p| p[1].clone())
        .unwrap()
}

#[test]
fn no_drift_read_when_the_caller_turns_it_off() {
    let ms = memories();
    let (h, log) = run(&ms[1], 1.0, &[], 1, None, false);
    assert!(log.is_empty());
    assert_eq!(field(&h, "path_drift_checked"), 0);
    assert_eq!(
        field(&h, "path_drift_claim_anchored_missing_paths"),
        json!([])
    );
}

#[test]
fn the_origin_root_goes_only_to_a_read_without_an_anchor() {
    let ms = memories();
    let (_, log) = run(&ms[1], 1.0, &[], 1, None, true);
    assert_eq!(log[0][5], "/w/t");
    let a = Anchor {
        root: None,
        rule: "no_live_checkout".into(),
    };
    let (_, log) = run(&ms[1], 1.0, &[], 1, Some(&a), true);
    assert_eq!(log[0][5], Value::Null);
    assert_eq!(log[0][6], json!([null, "no_live_checkout"]));
    // An empty root is passed as it is; no origin passes None.
    let (_, log) = run(&ms[3], 1.0, &[], 1, None, true);
    assert_eq!(log[0][5], "");
    let (_, log) = run(&ms[0], 1.0, &[], 1, None, true);
    assert_eq!(log[0][5], Value::Null);
}

#[test]
fn the_attestation_legs_are_the_records_own() {
    let ms = memories();
    let (_, log) = run(&ms[1], 1.0, &[], 1, None, true);
    // Targets present: the literal targets, nulls dropped, in map order.
    assert_eq!(log[0][1], "LiteralPaths");
    assert_eq!(log[0][2], json!(["/w/t/src/a.py", "z"]));
    assert_eq!(log[0][4], json!(["/w/t/y"]));
    let (_, log) = run(&ms[0], 1.0, &[], 1, None, true);
    assert_eq!(log[0][1], "ReferencePaths");
    assert_eq!(log[0][2], json!(["src/a.py"]));
    assert_eq!(log[0][4], json!(["gone.py"]));
}

#[test]
fn each_bucket_rides_its_own_field() {
    let ms = memories();
    let (h, _) = run(&ms[0], 1.0, &[], 1, None, true);
    for (name, want) in [
        ("path_drift_checked", json!(2)),
        ("path_drift_missing", json!(1)),
        ("path_drift_checked_paths", json!(["c1", "c2"])),
        ("path_drift_missing_paths", json!(["m1"])),
        ("path_drift_verified_paths", json!(["v1"])),
        ("path_drift_expected_absent_paths", json!(["e1"])),
        ("path_drift_present_but_attested_absent", json!(["p1"])),
        ("path_drift_dropped_as_route_paths", json!(["d1"])),
        ("path_drift_claim_anchored_missing_paths", json!(["k1"])),
    ] {
        assert_eq!(field(&h, name), want, "{name}");
    }
}

#[test]
fn the_score_rounds_half_to_even_on_its_exact_value() {
    let ms = memories();
    for (score, want) in [
        (0.03125, 0.0312),
        (-0.03125, -0.0312),
        (0.09375, 0.0938),
        (1.03125, 1.0312),
        (0.00005, 0.0001),
        (0.00015, 0.0001),
    ] {
        let (h, _) = run(&ms[0], score, &[], 0, None, false);
        assert_eq!(
            field(&h, "score")["repr"],
            json!(format!("{want}")),
            "{score}"
        );
    }
}

#[test]
fn relevance_at_its_bounds() {
    let ms = memories();
    for (matched, qu, want) in [
        (&["a", "b", "c"][..], 4, "high"),
        (&["a", "b"][..], 5, "medium"),
        (&["a"][..], 3, "low"),
        (&["a"][..], 0, "low"),
        (&["a", "b", "c"][..], 1, "high"),
    ] {
        let (h, _) = run(&ms[2], 0.5, matched, qu, None, false);
        assert_eq!(field(&h, "relevance"), want, "{matched:?} of {qu}");
        assert_eq!(field(&h, "query_unique"), qu);
    }
}

#[test]
fn the_snippet_budget_is_two_hundred_characters() {
    // `w ` * 150 with nothing matched: 202 characters at a budget of 200
    // (the window and its ellipsis), 200 at 199.
    let mut payload = common::jsonl("search_hit.jsonl")[0]["memories"][0].clone();
    payload["body"] = json!("w ".repeat(150));
    let m = memory(&payload);
    let (h, _) = run(&m, 1.0, &[], 0, None, false);
    let snippet = field(&h, "snippet");
    assert_eq!(snippet.as_str().unwrap().chars().count(), 202);
    // `abcd ` * 60 matched on `abcd`: 202 at 200, 197 at 199.
    payload["body"] = json!("abcd ".repeat(60));
    let m = memory(&payload);
    let (h, _) = run(&m, 1.0, &["abcd"], 1, None, false);
    let snippet = field(&h, "snippet");
    assert_eq!(snippet.as_str().unwrap().chars().count(), 202);
}

struct Raising;

impl Drift for Raising {
    fn detect(
        &mut self,
        _: &[u32],
        _: &bettermemory_core::attestation_paths::Paths,
        _: &bettermemory_core::attestation_paths::Paths,
        _: Option<&[u32]>,
        _: Option<&dyn bettermemory_core::verify_anchoring::passes::ReadAnchor>,
    ) -> Result<bettermemory_core::verify_anchoring::report::PathDriftReport, DriftError> {
        Err(DriftError::Scan(ScanError::NotPorted))
    }
}

#[test]
fn what_the_drift_read_raises_escapes_only_when_it_is_made() {
    let ms = memories();
    let scored = Scored {
        memory: &ms[0],
        score: 1.0,
        matched: &[],
        query_unique: 0,
    };
    let memo = SnippetMemo::default();
    assert!(build_hit(&memo, &mut Raising, &scored, None, true).is_err());
    assert!(build_hit(&memo, &mut Raising, &scored, None, false).is_ok());
}

#[test]
fn claim_anchors_prime_once_then_ask_each_record() {
    let ms = memories();
    let page = vec![ms[3].clone(), ms[1].clone(), ms[3].clone(), ms[2].clone()];
    let id = |m: &Memory| {
        m.fields
            .id
            .iter()
            .map(|&c| char::from_u32(c).unwrap())
            .collect::<String>()
    };
    let mut t = Table {
        table: json!({ id(&ms[3]): ["/p", "project_checkout"] }),
        log: Vec::new(),
    };
    let got = claim_anchors(Some(&mut t as &mut dyn RecordAnchors), &page);
    assert_eq!(
        got.iter().map(anchor_desc).collect::<Vec<_>>(),
        vec![
            json!(["/p", "project_checkout"]),
            json!(null),
            json!(["/p", "project_checkout"]),
            json!(null)
        ]
    );
    assert_eq!(t.log.iter().filter(|e| e[0] == "prime").count(), 1);
    assert_eq!(
        t.log[0][1],
        json!([id(&ms[3]), id(&ms[1]), id(&ms[3]), id(&ms[2])])
    );
    assert_eq!(t.log[1], json!(["for_record", id(&ms[3]), ""]));
    assert_eq!(t.log[2], json!(["for_record", id(&ms[1]), "/w/t"]));
    assert_eq!(t.log[4], json!(["for_record", id(&ms[2]), null]));
    let none = claim_anchors(None, &page);
    assert_eq!(none.len(), 4);
    assert!(none.iter().all(Option::is_none));
}
