//! Cases a plausible wrong port of the read passes fails: the budget
//! counted at the match, the dedup by spelling, the miss rule in the
//! other order, the parent rule left out, the filter loosened.

mod common;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::passes::{
    is_checkable_citation, read_attestations, relative_attestation_keys, AnchoredRead, ReadAnchor,
};
use bettermemory_core::verify_candidates::MAX_ANCHORED_CITATION_STATS;
use common::attestation_paths::{cps, Scripted};
use common::verify_anchoring_attest::paths;
use common::verify_anchoring_passes::{run, Logged, Pass, Stub};

fn spans_of(body: &[u32], words: &[&str]) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let mut from = 0;
    for w in words {
        let w = cps(w);
        let at = (from..=body.len() - w.len())
            .find(|&i| body[i..i + w.len()] == w[..])
            .unwrap();
        out.push((at, at + w.len()));
        from = at + w.len();
    }
    out
}

fn stub(root: &str, rule: &str, convicting: bool) -> Stub {
    Stub {
        root: Some(cps(root)),
        rule: rule.into(),
        convicting,
    }
}

fn stats(reads: &Logged) -> usize {
    reads.log.iter().filter(|e| e[0] == u32::from(b'e')).count()
}

#[test]
fn filtered_matches_do_not_spend_the_budget() {
    let mut words: Vec<String> = (0..40).map(|i| format!("run{i}.py")).collect();
    words.push("d/x.py".into());
    let body = cps(&words.join(" "));
    let refs: Vec<&str> = words.iter().map(String::as_str).collect();
    let m = spans_of(&body, &refs);
    let (r, out, _) = run(false, Pass::CheckCitations(&cps("/r"), &body), &m, &[]);
    r.unwrap();
    assert_eq!(out.missing, vec![cps("/r/d/x.py")]);
}

#[test]
fn seen_citations_do_not_spend_the_budget_and_the_budget_stops() {
    let words: Vec<String> = (0..20).map(|i| format!("d/f{i}.py")).collect();
    let body = cps(&words.join(" "));
    let refs: Vec<&str> = words.iter().map(String::as_str).collect();
    let m = spans_of(&body, &refs);
    let pre: Vec<PyStr> = (0..5).map(|i| cps(&format!("/r/d/F{i}.py"))).collect();
    let (_, out, reads) = run(false, Pass::CheckCitations(&cps("/r"), &body), &m, &pre);
    assert_eq!(stats(&reads), MAX_ANCHORED_CITATION_STATS);
    assert_eq!(out.checked.first(), Some(&cps("/r/d/f5.py")));
    assert_eq!(out.checked.len(), MAX_ANCHORED_CITATION_STATS);
}

#[test]
fn a_missing_parent_drops_the_citation() {
    let body = cps("nod/x.py d/y.py");
    let m = spans_of(&body, &["nod/x.py", "d/y.py"]);
    let (_, out, _) = run(false, Pass::CheckCitations(&cps("/r"), &body), &m, &[]);
    assert_eq!(out.checked, vec![cps("/r/d/y.py")]);
    let a = stub("/r", "home_checkout", true);
    let (_, out, _) = run(false, Pass::ReadCitations(&a, &body), &m, &[]);
    assert_eq!(out.missing, vec![cps("/r/d/y.py")]);
}

#[test]
fn the_miss_rule_asks_conviction_before_sparsity() {
    let body = cps("sp/x.py");
    let m = spans_of(&body, &["sp/x.py"]);
    let a = stub("/r", "standin", false);
    let (_, out, reads) = run(false, Pass::ReadCitations(&a, &body), &m, &[]);
    assert_eq!(out.reasons, vec!["standin".to_owned()]);
    assert!(reads.log.iter().all(|e| e[0] != u32::from(b's')));
    let a = stub("/r", "home_checkout", true);
    let (_, out, _) = run(false, Pass::ReadCitations(&a, &body), &m, &[]);
    assert_eq!(out.reasons, vec!["sparse_checkout".to_owned()]);
    assert!(out.missing.is_empty());
}

#[test]
fn an_attested_absence_is_read_by_key_and_present_absence_convicts() {
    let a = stub("/r", "home_checkout", true);
    let vp = paths("P", &[cps("src/ok.py")]);
    let ap = paths("P", &[cps("SRC/OK.PY"), cps("SRC/GONE.py")]);
    let (_, out, _) = run(false, Pass::ReadAttestations(&a, &vp, &ap), &[], &[]);
    assert_eq!(out.present_absent, vec![cps("/r/src/ok.py")]);
    assert_eq!(out.expected_absent, vec![cps("/r/SRC/GONE.py")]);
    assert!(out.verified.is_empty());
    let a = stub("/r", "standin", false);
    let (_, out, _) = run(false, Pass::ReadAttestations(&a, &vp, &ap), &[], &[]);
    assert_eq!(out.unconfirmed, vec![cps("/r/src/ok.py")]);
    assert!(out.present_absent.is_empty());
}

#[test]
fn an_absolute_rule_outside_the_root_reads_nothing() {
    let a = stub("/r", "home_checkout", true);
    let vp = paths(
        "P",
        &[cps("/ab/x/gone"), cps("/r/ab/gone"), cps("/un/gone")],
    );
    let ap = paths("P", &[]);
    let (_, out, _) = run(false, Pass::ReadAttestations(&a, &vp, &ap), &[], &[]);
    assert_eq!(out.missing, vec![cps("/r/ab/gone")]);
    assert_eq!(out.unconfirmed, vec![cps("/un/gone")]);
    assert_eq!(out.reasons, vec!["unanchored".to_owned()]);
}

#[test]
fn the_recorded_root_pass_is_inert_without_a_root_and_dedupes() {
    let vp = paths(
        "P",
        &[cps("a/gone.py"), cps("A/GONE.py"), cps("/r/a/gone.py")],
    );
    let ap = paths("P", &[]);
    let (_, out, _) = run(false, Pass::CheckAttestations(None, &vp, &ap), &[], &[]);
    assert_eq!(out, AnchoredRead::default());
    let (_, out, _) = run(
        false,
        Pass::CheckAttestations(Some(&cps("/r/")), &vp, &ap),
        &[],
        &[],
    );
    assert_eq!(out.missing, vec![cps("/r/a/gone.py")]);
    assert_eq!(out.claim_anchored, out.missing);
}

#[test]
fn the_citation_filter() {
    for (s, want) in [
        ("a/b.py", true),
        (".github/x.yml", true),
        ("x/.a.py", true),
        ("a/b.PY", true),
        ("a.py", false),
        ("a.b/c.py", false),
        ("path/to/x.py", false),
        ("a/b.pyc", false),
        ("a/b.tar.gz", false),
        ("a/b", false),
    ] {
        assert_eq!(is_checkable_citation(&cps(s)), want, "{s}");
    }
    // U+212A KELVIN SIGN lowers to `k`: `str.lower` of the extension.
    let mut s = cps("a/b.");
    s.push(0x212a);
    assert!(!is_checkable_citation(&s));
    let mut s = cps("a/b.loc");
    s.push(0x212a);
    assert!(is_checkable_citation(&s));
    let mut s = cps("a/b.");
    s.extend([0x0130, u32::from(b'n'), u32::from(b'i')]);
    // U+0130 lowers to `i` and a combining dot: not `ini`.
    assert!(!is_checkable_citation(&s));
}

#[test]
fn relative_keys_drop_the_empty_key_and_absolute_spellings() {
    let host = Scripted { nt: false };
    let mut reads = Logged::default();
    let vp = paths("P", &[cps("zz/a.py"), cps("/abs.py"), cps("b.py")]);
    let ap = paths("P", &[cps("C.py")]);
    let got = relative_attestation_keys(&host, &mut reads, &cps("/r"), &vp, &ap).unwrap();
    let mut got: Vec<PyStr> = got.into_iter().collect();
    got.sort();
    assert_eq!(got, vec![cps("/r/b.py"), cps("/r/c.py")]);
}

/// An anchor whose absolute spelling `/v/x` reads through a view to `/u/x`,
/// a path it cannot read when spelled directly.
struct Viewed;

impl ReadAnchor for Viewed {
    fn root(&self) -> Option<PyStr> {
        Some(cps("/r"))
    }
    fn rule(&self) -> String {
        "standin".into()
    }
    fn convicting(&self) -> bool {
        false
    }
    fn absolute(&self, p: &[u32]) -> (Option<PyStr>, String) {
        if p == cps("/v/x").as_slice() {
            return (Some(cps("/u/x")), "standin".into());
        }
        (None, "unanchored".into())
    }
}

#[test]
fn an_unreadable_absence_does_not_attest_a_readable_target() {
    let host = Scripted { nt: false };
    let mut reads = Logged::default();
    let mut out = AnchoredRead::default();
    let vp = paths("P", &[cps("/v/x")]);
    let ap = paths("P", &[cps("/u/x")]);
    read_attestations(&host, &mut reads, &Viewed, &vp, &ap, &mut out).unwrap();
    assert!(out.expected_absent.is_empty());
    assert!(out.checked.is_empty());
    assert_eq!(out.unconfirmed, vec![cps("/u/x")]);
    assert_eq!(out.reasons, vec!["standin".to_owned()]);
}
