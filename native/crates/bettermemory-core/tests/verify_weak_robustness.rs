//! Hostile inputs for the weak tier: a long patch stream, many claims,
//! the window at its bound; no panic, time linear in the stream.

mod timing;
mod verify_weak_script;

use std::time::Duration;

use bettermemory_core::claims::{Claim, Kind};
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::literal_lines::LiteralReader;
use bettermemory_core::verify::weak::{
    weak_tier_evaluation, Place, WeakError, MAX_PATCH_STREAM_COMMITS,
};
use serde_json::json;
use verify_weak_script::{literal, Script};

fn stream(commits: usize) -> String {
    let mut out = String::new();
    for i in 0..commits {
        out.push_str(&format!(
            "\u{1}{i:040x}\ndiff --git a/m{i}.py b/m{i}.py\n--- a/m{i}.py\n+++ b/m{i}.py\n@@ -1 +1 @@\n-X{i} = 1\n+X{i} = 2\n"
        ));
    }
    out
}

fn run(commits: usize, claims: &[Claim]) -> Duration {
    let script = Script::new(&json!({"stream": stream(commits)}));
    let lit = LiteralReader::default();
    let cwd = cps("/w");
    let at = Place {
        cwd: &cwd,
        toplevel: None,
    };
    let shas: Vec<Vec<u32>> = (0..commits.min(MAX_PATCH_STREAM_COMMITS))
        .map(|i| cps(&format!("{i:040x}")))
        .collect();
    timing::once(|| {
        weak_tier_evaluation(
            &script,
            at,
            &shas,
            &[cps("m0.py")],
            claims,
            None,
            &literal,
            &lit,
        )
        .expect("an answer")
    })
}

#[test]
fn many_claims_over_a_full_window() {
    let claims: Vec<Claim> = (0..64)
        .map(|i| Claim {
            kind: [Kind::Literal, Kind::Path, Kind::Absent, Kind::Symbol][i % 4],
            rel_path: cps(&format!("m{i}.py")),
            name: cps(&format!("X{i}")),
            value: cps("1"),
        })
        .collect();
    run(MAX_PATCH_STREAM_COMMITS, &claims);
}

#[test]
fn time_is_linear_in_the_stream() {
    let claims = [Claim {
        kind: Kind::Absent,
        rel_path: cps("m"),
        name: Vec::new(),
        value: Vec::new(),
    }];
    timing::grows_within("weak tier", 30, &mut || run(500, &claims), &mut || {
        run(5000, &claims)
    });
}

#[test]
fn a_deep_absent_directory_and_a_huge_hunk_count() {
    let claims = [Claim {
        kind: Kind::Absent,
        rel_path: cps(&"d/".repeat(10_000)),
        name: Vec::new(),
        value: Vec::new(),
    }];
    run(10, &claims);
    let big = format!(
        "\u{1}{}\ndiff --git a/m.py b/m.py\n@@ -1 +1,{} @@\n",
        "a".repeat(40),
        "9".repeat(18)
    );
    let script = Script::new(&json!({"stream": big}));
    let lit = LiteralReader::default();
    let cwd = cps("/w");
    let at = Place {
        cwd: &cwd,
        toplevel: None,
    };
    let got = weak_tier_evaluation(
        &script,
        at,
        &[cps(&"a".repeat(40))],
        &[cps("m.py")],
        &claims,
        None,
        &literal,
        &lit,
    );
    assert!(got.is_ok());
}

/// A hunk count past the 4,300 digits `int()` reads: the reference raises
/// ValueError from `build_binding_index`; the port answers `IntTooLong`
/// through `weak_tier_evaluation`.
#[test]
fn a_hunk_count_past_the_int_digit_limit_raises() {
    let claims = [Claim {
        kind: Kind::Absent,
        rel_path: cps("gone.py"),
        name: Vec::new(),
        value: Vec::new(),
    }];
    let big = format!(
        "\u{1}{}\ndiff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +1,{} @@\n",
        "a".repeat(40),
        "9".repeat(4301)
    );
    let script = Script::new(&json!({"stream": big}));
    let lit = LiteralReader::default();
    let cwd = cps("/w");
    let at = Place {
        cwd: &cwd,
        toplevel: None,
    };
    let got = weak_tier_evaluation(
        &script,
        at,
        &[cps(&"a".repeat(40))],
        &[cps("m.py")],
        &claims,
        None,
        &literal,
        &lit,
    );
    assert!(matches!(got, Err(WeakError::IntTooLong)));
}
