//! Cases a plausible wrong port of the weak tier and the narrowing fails:
//! the window bound, the claim order of the drifted renders, the window
//! intersection, the strict date order, a naive stamp read as UTC, the
//! lone surrogate a claim path may hold, the legs' first-seen order.

mod verify_weak_script;

use bettermemory_core::claims::{Claim, Kind};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_history::ReachableWalk;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::legs::{claim_legs, resolve_commit_drift, Reads};
use bettermemory_core::verify::literal_lines::LiteralReader;
use bettermemory_core::verify::weak::{weak_tier_evaluation, Place, Set, MAX_PATCH_STREAM_COMMITS};
use bettermemory_core::verify_basis::{BASIS_AUTHOR_DATE, BASIS_REACHABILITY};
use serde_json::{json, Value};
use verify_weak_script::{literal, Script};

const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn stream() -> String {
    format!(
        "\u{1}{A}\ndiff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-TIMEOUT = 30\n+TIMEOUT = 45\n@@ -9,0 +10 @@\n+def handler(e):\n"
    )
}

fn claim(kind: Kind, path: &str, name: &str, value: &str) -> Claim {
    Claim {
        kind,
        rel_path: cps(path),
        name: cps(name),
        value: cps(value),
    }
}

fn weak(s: Value, shas: &[&str], claims: &[Claim]) -> (Vec<Vec<u32>>, Option<Set>) {
    let script = Script::new(&s);
    let lit = LiteralReader::default();
    let cwd = cps("/w");
    let at = Place {
        cwd: &cwd,
        toplevel: None,
    };
    let shas: Vec<Vec<u32>> = shas.iter().map(|s| cps(s)).collect();
    weak_tier_evaluation(
        &script,
        at,
        &shas,
        &[cps("m.py")],
        claims,
        None,
        &literal,
        &lit,
    )
    .expect("an answer")
}

#[test]
fn the_window_bound_is_inclusive() {
    let s = json!({"stream": stream()});
    let lit = [claim(Kind::Literal, "m.py", "TIMEOUT", "30")];
    let at_bound: Vec<String> = (0..MAX_PATCH_STREAM_COMMITS)
        .map(|i| format!("{i:040x}"))
        .collect();
    let refs: Vec<&str> = at_bound.iter().map(String::as_str).collect();
    assert!(weak(s.clone(), &refs, &lit).1.is_some());
    let mut over = refs.clone();
    over.push(A);
    assert_eq!(weak(s, &over, &lit), (Vec::new(), None));
}

#[test]
fn drifted_keeps_claim_order_and_the_window_bounds_implication() {
    let claims = [
        claim(Kind::Symbol, "m.py", "handler", ""),
        claim(Kind::Literal, "m.py", "TIMEOUT", "30"),
    ];
    let (drifted, implicated) = weak(json!({"stream": stream()}), &[A, B], &claims);
    assert_eq!(drifted, vec![cps("m.py::handler"), cps("m.py::TIMEOUT=30")]);
    assert_eq!(implicated, Some([cps(A)].into_iter().collect()));
    // A commit the claims implicate outside the window is dropped.
    let (_, implicated) = weak(json!({"stream": stream()}), &[B], &claims);
    assert_eq!(implicated, Some(Set::new()));
}

#[test]
fn a_blank_split_stream_is_still_read() {
    let s = json!({"stream": null, "split": ["", [B], ["m.py"], ["m.py"]]});
    let (drifted, implicated) = weak(s, &[A, B], &[claim(Kind::Path, "m.py", "", "")]);
    assert_eq!(drifted, vec![cps("m.py")]);
    assert_eq!(implicated, Some([cps(A), cps(B)].into_iter().collect()));
}

fn dates(s: Value, since: &str, claims: &[Claim]) -> Value {
    let script = Script::new(&s);
    let lit = LiteralReader::default();
    let cwd = cps("/w");
    let r = Reads {
        git: &script,
        at: Place {
            cwd: &cwd,
            toplevel: None,
        },
        literal_pathspecs: &literal,
        lit: &lit,
    };
    let since = DateTime::from_isoformat(&cps(since)).expect("a stamp");
    let got = resolve_commit_drift(r, &since, 9, &[cps("u.py")], claims, None, &mut || None);
    match got {
        Ok(Some(d)) => json!([d.count, d.claims_checked, d.basis]),
        Ok(None) => Value::Null,
        Err(e) => json!(format!("{e:?}")),
    }
}

#[test]
fn the_date_order_is_strict_and_a_naive_stamp_is_utc() {
    let c = [claim(Kind::Path, "q.py", "", "")];
    let pairs = json!({"pairs": [[["2026-01-01T10:00:00+00:00", A], ["2026-01-01T10:00:00.000001+00:00", B]], []]});
    assert_eq!(
        dates(pairs.clone(), "2026-01-01T10:00:00+00:00", &c),
        json!([1, 1, BASIS_AUTHOR_DATE])
    );
    assert_eq!(
        dates(pairs.clone(), "2026-01-01T10:00:00", &c),
        json!([1, 1, BASIS_AUTHOR_DATE])
    );
    assert_eq!(
        dates(pairs, "2026-01-01T12:00:00+02:00", &c),
        json!([1, 1, BASIS_AUTHOR_DATE])
    );
}

#[test]
fn a_lone_surrogate_keeps_the_conservative_count() {
    let mut c = claim(Kind::Path, "a", "", "");
    c.rel_path.push(0xDC80);
    assert_eq!(
        dates(json!({}), "2026-01-01T10:00:00+00:00", &[c.clone()]),
        json!([9, 1, BASIS_AUTHOR_DATE])
    );
    let script = Script::new(&json!({}));
    let lit = LiteralReader::default();
    let cwd = cps("/w");
    let r = Reads {
        git: &script,
        at: Place {
            cwd: &cwd,
            toplevel: None,
        },
        literal_pathspecs: &literal,
        lit: &lit,
    };
    let walk = ReachableWalk {
        anchor: cps(A),
        head: cps(B),
        commits: vec![cps(A), cps(B)],
        touched: Default::default(),
        listed: true,
        timed_out: false,
    };
    let since = DateTime::from_isoformat(&cps("2026-01-01")).expect("a stamp");
    let got = resolve_commit_drift(r, &since, 9, &[], &[c], Some(&walk), &mut || None)
        .expect("an answer")
        .expect("a count");
    assert_eq!((got.count, got.basis.as_str()), (2, BASIS_REACHABILITY));
    assert!(script.calls.borrow().is_empty());
}

#[test]
fn the_legs_keep_first_seen_order() {
    let script = Script::new(&json!({"rp": ["z.py", "a.py", "z.py"]}));
    let lit = LiteralReader::default();
    let cwd = cps("/w");
    let r = Reads {
        git: &script,
        at: Place {
            cwd: &cwd,
            toplevel: None,
        },
        literal_pathspecs: &literal,
        lit: &lit,
    };
    let claims = [
        claim(Kind::Path, "b.py", "", ""),
        claim(Kind::Literal, "c.py", "X", "[1]"),
    ];
    let (u, g) = claim_legs(r, &[cps("x")], &claims)
        .expect("an answer")
        .expect("legs");
    assert_eq!(u, vec![cps("z.py"), cps("a.py"), cps("c.py")]);
    assert_eq!(g, vec![cps("b.py")]);
}
