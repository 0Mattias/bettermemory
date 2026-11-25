//! Hostile inputs to the supersession detector: a digest of 20,000 shares,
//! bodies of 100,000 words, 1,000 records tied in score, integer extremes
//! in a share. None may panic, in debug or release, and each grows with
//! its input as the reference's loops do (tests/timing).

mod common;
mod timing;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::search_dedup::RawTokenCache;
use bettermemory_core::store::digest_query::DigestShare;
use bettermemory_core::supersession::{
    detect_supersession, detect_supersession_in, DetectError, SupersessionReport,
};
use bettermemory_core::unicode::cps;
use common::supdetect::{derived_shares, share, text, Fake, Rec};
use serde_json::{json, Value};

const PORT_OLD: &str = "The auth service listens on port 8443 inside the cluster; the ingress \
                        terminates TLS and forwards to that port.";
const PORT_NEW: &str = "The auth service moved to port 9443 when the sidecar was introduced; \
                        the ingress forwards to 9443 and the old port is closed.";

fn cluster(name: &str) -> String {
    format!("The auth service listens on port 8443 inside cluster {name}.")
}

fn digest(store: &mut Fake) -> Result<SupersessionReport, DetectError> {
    let t = store.t.clone();
    detect_supersession_in(&RawTokenCache::default(), store, &t, &[])
}

/// Six restatements, then `extra` shares whose bound is under every
/// score and whose tokens are gone.
fn wide_store(extra: usize) -> Fake {
    let recs: Vec<(String, String)> = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
        .iter()
        .enumerate()
        .map(|(i, n)| (format!("m{i:02}"), cluster(n)))
        .collect();
    let mut shares = derived_shares(PORT_NEW, &recs);
    shares.extend((0..extra).map(|i| share(&format!("f{i:05}"), 12, false, 3)));
    Fake::new(PORT_NEW, recs, Some(shares))
}

#[test]
fn twenty_thousand_shares_full_after_the_first_batch() {
    let mut store = wide_store(20_000);
    let r = digest(&mut store).expect("a report");
    assert_eq!(r.supersedes.len(), 5);
    let tokens: Vec<&Value> = store
        .log
        .iter()
        .filter(|l| l[0] == json!("tokens"))
        .collect();
    assert_eq!(tokens.len(), 1, "one batch's tokens read");
    assert_eq!(tokens[0][1].as_array().map(Vec::len), Some(64));
    timing::grows_within(
        "20,000 shares against 2,000",
        timing::TENFOLD,
        &mut || {
            let mut store = wide_store(2_000);
            timing::once(|| digest(&mut store))
        },
        &mut || {
            let mut store = wide_store(20_000);
            timing::once(|| digest(&mut store))
        },
    );
}

/// A claim-sized body of `words` words: one cue clause, then the rest.
fn long_body(words: usize, port: &str, cue: bool) -> String {
    let head = if cue {
        format!("The auth service moved to port {port}; ")
    } else {
        format!("The auth service listens on port {port}; ")
    };
    let rest: Vec<String> = (0..words / 4)
        .map(|_| format!("ingress forwards port {port}"))
        .collect();
    format!("{head}{}.", rest.join(" "))
}

#[test]
fn bodies_of_a_hundred_thousand_words() {
    let run = |words: usize| {
        let new = long_body(words, "9443", true);
        let old = vec![Rec::new("m00", &long_body(words, "8443", false))];
        detect_supersession(&RawTokenCache::default(), &cps(&new), &old, &[]).expect("a report")
    };
    let r = run(100_000);
    assert!(r.eligible);
    assert_eq!(r.supersedes.len(), 1);
    assert_eq!(text(&r.supersedes[0].old_value), "8443");
    timing::grows_within(
        "100,000 words against 10,000",
        timing::TENFOLD,
        &mut || timing::once(|| run(10_000)),
        &mut || timing::once(|| run(100_000)),
    );
}

#[test]
fn a_thousand_records_tied_in_score() {
    let make = |n: usize| -> Vec<(String, String)> {
        (0..n)
            .rev()
            .map(|i| (format!("t{i:04}"), PORT_OLD.to_string()))
            .collect()
    };
    let recs = make(1_000);
    let rows: Vec<Rec> = recs.iter().map(|(i, b)| Rec::new(i, b)).collect();
    let r = detect_supersession(&RawTokenCache::default(), &cps(PORT_NEW), &rows, &[])
        .expect("a report");
    let want: Vec<String> = (0..5).map(|i| format!("t{i:04}")).collect();
    let got: Vec<String> = r.supersedes.iter().map(|m| text(&m.memory_id)).collect();
    assert_eq!(got, want);
    let mut store = Fake::new(
        PORT_NEW,
        recs.clone(),
        Some(derived_shares(PORT_NEW, &recs)),
    );
    let r = digest(&mut store).expect("a report");
    let got: Vec<String> = r.supersedes.iter().map(|m| text(&m.memory_id)).collect();
    assert_eq!(got, want);
    timing::grows_within(
        "1,000 tied records against 100",
        timing::TENFOLD,
        &mut || {
            let recs = make(100);
            let mut store = Fake::new(
                PORT_NEW,
                recs.clone(),
                Some(derived_shares(PORT_NEW, &recs)),
            );
            timing::once(|| digest(&mut store))
        },
        &mut || {
            let recs = make(1_000);
            let mut store = Fake::new(
                PORT_NEW,
                recs.clone(),
                Some(derived_shares(PORT_NEW, &recs)),
            );
            timing::once(|| digest(&mut store))
        },
    );
}

fn with_claim(claim: PyValue, shared: i64) -> DigestShare {
    DigestShare {
        id: "m00".into(),
        n: PyValue::Int("0".into()),
        claim,
        stamped: false,
        shared,
    }
}

#[test]
fn integer_extremes_in_a_share() {
    let recs = vec![("m00".to_string(), PORT_OLD.to_string())];
    let ints = [
        i64::MAX.to_string(),
        i64::MIN.to_string(),
        "0".to_string(),
        "-1".to_string(),
    ];
    for claim in &ints {
        for shared in [i64::MAX, i64::MIN, 0, 1, 5] {
            let shares = vec![with_claim(PyValue::Int(claim.clone()), shared)];
            let mut store = Fake::new(PORT_NEW, recs.clone(), Some(shares));
            digest(&mut store).expect("a report");
        }
    }
    // A claim past `i64`, or not an int, is declined; a bool is 0 or 1.
    let declined = [
        PyValue::Int("1".repeat(30)),
        PyValue::Int(format!("-{}", "9".repeat(40))),
        PyValue::None,
        PyValue::Float(9.0),
        PyValue::str("9"),
    ];
    for claim in declined {
        let mut store = Fake::new(PORT_NEW, recs.clone(), Some(vec![with_claim(claim, 5)]));
        let e = digest(&mut store).expect_err("declined");
        assert_eq!(e.class(), "NotPorted");
    }
    for b in [true, false] {
        let mut store = Fake::new(
            PORT_NEW,
            recs.clone(),
            Some(vec![with_claim(PyValue::Bool(b), 5)]),
        );
        digest(&mut store).expect("a report");
    }
    // No share at all, and every share under the floor.
    let mut store = Fake::new(PORT_NEW, recs.clone(), Some(Vec::new()));
    assert!(digest(&mut store).expect("a report").supersedes.is_empty());
    let low = (0..200)
        .map(|i| share(&format!("f{i}"), 1_000, false, 1))
        .collect();
    let mut store = Fake::new(PORT_NEW, recs, Some(low));
    assert!(digest(&mut store).expect("a report").supersedes.is_empty());
    let tokens = store.log.iter().filter(|l| l[0] == json!("tokens")).count();
    assert_eq!(tokens, 0, "no batch under the floor is read");
}
