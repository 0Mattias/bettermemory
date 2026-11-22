//! Cases a plausible wrong drift reader fails: the pass order, the early
//! empty answer before the attestations are read, the deferral of a body
//! candidate to its relative attestation, the cited path (not the
//! target) in the route bucket, the anchored verdicts on an attested
//! absence, and the store questions of `holds_commit`.
#![cfg(unix)]

mod common;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::passes::ReadAnchor;
use common::attestation_paths::cps;
use common::verify_anchoring_attest::paths;
use common::verify_anchoring_detect::{drift, holds, report_json, repos, Extended};
use common::verify_anchoring_passes::Stub;
use serde_json::{json, Map, Value};

fn p(items: &[&str]) -> bettermemory_core::attestation_paths::Paths {
    paths("P", &items.iter().map(|s| cps(s)).collect::<Vec<PyStr>>())
}

fn report(
    body: &str,
    vp: &[&str],
    ap: &[&str],
    root: Option<&str>,
    anchor: Option<&Stub>,
    spans: &[(usize, usize)],
) -> Value {
    let root = root.map(cps);
    let anchor = anchor.map(|a| a as &dyn ReadAnchor);
    let (r, _) = drift(
        false,
        &cps(body),
        &p(vp),
        &p(ap),
        root.as_deref(),
        anchor,
        spans,
    );
    report_json(&r.unwrap())
}

fn stub(root: Option<&str>, rule: &str, convicting: bool) -> Stub {
    Stub {
        root: root.map(cps),
        rule: rule.into(),
        convicting,
    }
}

#[test]
fn an_attestation_lands_before_the_citation_of_the_same_file() {
    // `src/ok.py` is cited and attested: the attestation pass reads it
    // first and it is verified.
    let got = report(
        "see src/ok.py",
        &["src/ok.py"],
        &[],
        Some("/r"),
        None,
        &[(4, 13)],
    );
    assert_eq!(got["verified"], json!(["/r/src/ok.py"]));
    assert_eq!(got["checked"], json!(["/r/src/ok.py"]));
}

#[test]
fn nothing_to_read_answers_before_the_attestations_are_read() {
    // `~!` raises in `expanduser`; the empty body and dead root answer first.
    let (r, log) = drift(
        false,
        &[],
        &p(&["~!/x"]),
        &p(&[]),
        Some(&cps("/r/dead")),
        None,
        &[],
    );
    assert_eq!(report_json(&r.unwrap())["checked"], json!([]));
    assert_eq!(log.inner.log, vec![cps("l/r/dead")]);
    let (r, _) = drift(
        false,
        &[],
        &p(&["~!/x"]),
        &p(&[]),
        Some(&cps("/r")),
        None,
        &[],
    );
    assert!(r.is_err());
}

#[test]
fn a_body_candidate_defers_to_its_relative_attestation() {
    let got = report(
        "gone: /r/src/gone.py",
        &["src/gone.py"],
        &[],
        Some("/r"),
        None,
        &[],
    );
    assert_eq!(got["missing"], json!(["/r/src/gone.py"]));
    assert_eq!(got["claim_anchored_missing"], json!(["/r/src/gone.py"]));
    let bare = report("gone: /r/src/gone.py", &[], &[], Some("/r"), None, &[]);
    assert_eq!(bare["claim_anchored_missing"], json!([]));
}

#[test]
fn the_route_bucket_holds_the_cited_path() {
    let a = stub(Some("/r"), "home_checkout", true);
    let got = report("call /api/v1/x", &[], &[], None, Some(&a), &[]);
    assert_eq!(got["dropped_as_route"], json!(["/api/v1/x"]));
    let hq = report("call /hq/api/v1", &[], &[], None, Some(&a), &[]);
    assert_eq!(hq["dropped_as_route"], json!([]));
    assert_eq!(hq["missing"], json!(["/H/hq/api/v1"]));
    // A route target that is not the cited path: the bucket holds the
    // citation, `/mv/q`, not its target `/api/m/mv/q`.
    let moved = Extended(stub(Some("/r"), "home_checkout", true));
    let (r, _) = drift(
        false,
        &cps("call /mv/q"),
        &p(&[]),
        &p(&[]),
        None,
        Some(&moved),
        &[],
    );
    assert_eq!(
        report_json(&r.unwrap())["dropped_as_route"],
        json!(["/mv/q"])
    );
}

#[test]
fn a_present_attested_absence_convicts_only_where_the_read_does() {
    let conv = stub(Some("/r"), "home_checkout", true);
    let got = report("at /hq/ok", &[], &["/hq/ok"], None, Some(&conv), &[]);
    assert_eq!(got["present_but_attested_absent"], json!(["/H/hq/ok"]));
    let soft = stub(Some("/r"), "standin", false);
    let got = report("at /r/ok", &[], &["/r/ok"], None, Some(&soft), &[]);
    assert_eq!(got["present_but_attested_absent"], json!([]));
    assert_eq!(got["unconfirmed"], json!([["/r/ok", "standin"]]));
    assert_eq!(got["anchor"], json!("standin"));
}

#[test]
fn holds_commit_reads_the_store_at_the_root_only() {
    let mut w = Map::new();
    let empty = |w: &mut Map<String, Value>, k: &str| w.insert(k.into(), json!(""));
    w.insert("a/.git/HEAD".into(), json!(hex(b"ref: refs/heads/main\n")));
    empty(&mut w, "a/.git/objects/");
    empty(&mut w, "a/.git/refs/");
    empty(&mut w, "a/sub/");
    let fs = repos(&w);
    let sha = "0".repeat(40);
    assert_eq!(holds(&fs, "a", &sha), json!(false));
    assert_eq!(holds(&fs, "a/sub", &sha), json!(false));
    // An alternates entry holding a NUL: `realpath` raises ValueError out
    // of `has`, which `holds_commit` catches (the helper panics on a
    // caught class passed up).
    w.insert(
        "a/.git/objects/info/alternates".into(),
        json!(hex(b"/x\0y\n")),
    );
    let fs = repos(&w);
    assert_eq!(holds(&fs, "a", &sha), json!(false));
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}
