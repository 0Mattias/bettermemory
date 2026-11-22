//! Hostile input for the drift readers: a megabyte body, many
//! attestations, lone surrogates, a sha past every length: the
//! reference's answers (a lone surrogate in a report path declines), and
//! time that grows in proportion to the attestations.
#![cfg(unix)]

mod common;
mod timing;

use bettermemory_core::attestation_paths::Raised;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::detect::DriftError;
use bettermemory_core::verify_anchoring::passes::ReadAnchor;
use common::attestation_paths::cps;
use common::verify_anchoring_attest::paths;
use common::verify_anchoring_detect::{drift, holds, report_json, repos};
use common::verify_anchoring_passes::Stub;
use serde_json::{json, Map};

fn stub() -> Stub {
    Stub {
        root: Some(cps("/r")),
        rule: "home_checkout".into(),
        convicting: true,
    }
}

#[test]
fn a_megabyte_body_and_odd_code_points_answer_as_the_reference() {
    // The answers are the reference's on the same input (CPython 3.13,
    // about 0.02 seconds each); the release build runs them as well.
    let body = cps(&"/r/gone/x.py and src/a.py ".repeat(40_000));
    let odd: Vec<PyStr> = vec![vec![0xd800], vec![0], vec![0x10_ffff], cps("/r/\u{fffd}")];
    let (vp, none) = (paths("P", &odd), paths("P", &[]));
    let a = stub();
    let root = cps("/r");
    // The reference's citation spans over the bounded text: each `src/a.py`.
    let spans: Vec<(usize, usize)> = (0..1260).map(|i| (26 * i + 17, 26 * i + 25)).collect();
    let unanchored = |nt: bool| {
        if nt {
            json!({"checked": ["/r/gone/x.py"], "missing": ["/r/gone/x.py"],
                "verified": [], "expected_absent": [], "dropped_as_route": [],
                "claim_anchored_missing": []})
        } else {
            json!({"checked": ["/r/gone/x.py", "/r/src/a.py"],
                "missing": ["/r/gone/x.py", "/r/src/a.py"], "verified": [],
                "expected_absent": [], "dropped_as_route": [],
                "claim_anchored_missing": ["/r/src/a.py"]})
        }
    };
    let anchored = |nt: bool| {
        let (checked, missing) = if nt {
            (json!([]), json!([]))
        } else {
            (json!(["/r/src/a.py"]), json!(["/r/src/a.py"]))
        };
        json!({"checked": checked, "missing": missing.clone(), "verified": [],
            "expected_absent": [], "dropped_as_route": [],
            "claim_anchored_missing": missing, "anchor": "home_checkout",
            "unconfirmed": [["/r/gone/x.py", "standin"]],
            "present_but_attested_absent": []})
    };
    for nt in [false, true] {
        let (r, _) = drift(nt, &body, &none, &none, Some(&root), None, &spans);
        assert_eq!(report_json(&r.unwrap()), unanchored(nt));
        let (r, _) = drift(nt, &body, &none, &none, None, Some(&a), &spans);
        assert_eq!(report_json(&r.unwrap()), anchored(nt));
        // The reference's report holds `/r/\ud800` (an expected absence):
        // a report path holding a lone surrogate declines.
        for anchor in [None, Some(&a as &dyn ReadAnchor)] {
            let at = anchor.is_none().then_some(root.as_slice());
            let (r, _) = drift(nt, &body, &vp, &vp, at, anchor, &[(0, 3)]);
            assert_eq!(r.unwrap_err(), DriftError::Raised(Raised::NotPorted));
            // A body that starts with a lone surrogate has no candidate,
            // and a span past the bounded text is no citation.
            let (r, _) = drift(nt, &[0xd800, 0x2f], &none, &none, None, anchor, &[(9, 2)]);
            let got = report_json(&r.unwrap());
            for k in ["checked", "missing", "verified", "expected_absent"] {
                assert_eq!(got[k], json!([]), "{k}");
            }
        }
    }
    // The candidate scan stops at a lone surrogate: the reference reports
    // the path before it, and so does the port.
    let mut lone = cps("see /r/gone/");
    lone.push(0xdc80);
    lone.extend(cps(".py"));
    let (r, _) = drift(false, &lone, &none, &none, None, None, &[]);
    assert_eq!(
        report_json(&r.unwrap()),
        json!({"checked": ["/r/gone"], "missing": ["/r/gone"], "verified": [],
            "expected_absent": [], "dropped_as_route": [],
            "claim_anchored_missing": []})
    );
}

#[test]
fn odd_names_never_panic_the_store_question() {
    let mut w = Map::new();
    w.insert("a/.git/objects/".into(), json!(""));
    w.insert("a/.git/refs/".into(), json!(""));
    w.insert(
        "a/.git/HEAD".into(),
        json!("7265663a20726566732f68656164732f6d61696e0a"),
    );
    let fs = repos(&w);
    for sha in [
        "",
        "0",
        &"f".repeat(64),
        &"0".repeat(100_000),
        "\u{e9}\u{e9}",
    ] {
        assert_eq!(holds(&fs, "a", sha), json!(false));
    }
}

#[test]
fn the_reads_grow_with_the_attestations() {
    let entry = |i: usize| cps(&format!("/r/gone{i}.py"));
    let small: Vec<PyStr> = (0..300).map(entry).collect();
    let large: Vec<PyStr> = (0..3000).map(entry).collect();
    let (ps, pl, none) = (paths("P", &small), paths("P", &large), paths("P", &[]));
    let body = cps("see /r/x.py");
    let a = stub();
    timing::grows_within(
        "detect_path_drift unanchored",
        timing::TENFOLD,
        &mut || timing::once(|| drift(false, &body, &ps, &none, Some(&cps("/r")), None, &[])),
        &mut || timing::once(|| drift(false, &body, &pl, &none, Some(&cps("/r")), None, &[])),
    );
    timing::grows_within(
        "detect_path_drift anchored",
        timing::TENFOLD,
        &mut || timing::once(|| drift(false, &body, &ps, &ps, None, Some(&a), &[])),
        &mut || timing::once(|| drift(false, &body, &pl, &pl, None, Some(&a), &[])),
    );
}
