//! Every case of `verify_count.jsonl` (POSIX only) against
//! `verify::anchors` and `verify::count`, git scripted as the generator
//! scripts it (`tests/contract/gen_verify_count.py`).

#![cfg(unix)]

mod common;
mod verify_count_script;
mod verify_weak_script;

use bettermemory_core::attestation_paths::{Kind, Paths};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::origin_history::{HistoryUnread, Unwalked};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::anchors::commit_drift_anchor_paths;
use bettermemory_core::verify::count::{
    anchor_specs, applicable_unread, compute_commit_drift, quiescent_drift_applicable,
    resolve_commit_drift_count, CommitDrift, CountError, CountReads, Effects, Record,
};
use bettermemory_core::verify::literal_lines::LiteralReader;
use bettermemory_core::verify::weak::{Place, WeakError};
use bettermemory_core::verify_candidates::scan::UserHome;
use common::parity::Ledger;
use serde_json::{json, Value};
use verify_count_script::{walk, Counted, Walker};
use verify_weak_script::{claims, literal, out, outs, text, texts};

const FILE: &str = "verify_count.jsonl";
/// The home the compare form expands `~` to, pinned as HOME by the generator.
const HOME: &str = "/hm/u";

fn paths(vp: &Value, kind: &str) -> Paths {
    Paths {
        kind: match kind {
            "literal" => Kind::Literal,
            "reference" => Kind::Reference,
            _ => Kind::Plain,
        },
        items: texts(vp),
    }
}

fn status(d: CommitDrift) -> Value {
    match d {
        CommitDrift::None => Value::Null,
        CommitDrift::Unread(w) => json!(["unread", w.value()]),
        CommitDrift::Status(s) => json!([
            s.status,
            s.commits_since_verify,
            s.recommendation.as_deref().map_or(Value::Null, out),
            s.claims_checked,
            outs(&s.claims_drifted),
            s.basis
        ]),
    }
}

fn error(e: CountError) -> Value {
    match e {
        CountError::Weak(WeakError::NaiveAware) => json!("TypeError"),
        CountError::Weak(WeakError::IntTooLong) => json!("ValueError"),
        e => panic!("no answer: {e:?}"),
    }
}

fn ccd(case: &Value, git: &Counted) -> Value {
    let home = cps(HOME);
    let vp = paths(&case["vp"], "plain");
    let body = text(&case["body"]);
    let stored = texts(&case["claims"]);
    let vh = case["vh"].as_str().map(cps);
    let record = Record {
        last_verified_at: case["lva"]
            .as_str()
            .map(|t| DateTime::from_isoformat(&cps(t)).expect("a stamp")),
        verified_paths: &vp,
        body: &body,
        claims: &stored,
        verified_head: vh.as_deref(),
    };
    let lit = LiteralReader::default();
    let root = case.get("root").map(|r| r.as_str().map(cps));
    let commit_root = |_: Option<&[u32]>| root.clone().flatten();
    let unmapped = case["s"]
        .get("unmapped")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let project = |p: &[PyStr]| (p.to_vec(), unmapped);
    let mut walker = Walker(git);
    let mut unwalked = Unwalked::new();
    let mut timestamps = |cwd: &[u32]| git.timestamps(cwd);
    let mut lookup = |_: &[u32]| UserHome::Unknown;
    let mut fx = Effects {
        git,
        literal_pathspecs: &literal,
        lit: &lit,
        origin: &mut walker,
        unwalked: &mut unwalked,
        timestamps: &mut timestamps,
        commit_root: root.is_some().then_some(&commit_root as _),
        project: &project,
        home: Some(&home),
        lookup: &mut lookup,
    };
    compute_commit_drift(&mut Scope::default(), &record, &mut fx).map_or_else(error, status)
}

/// The answer and the calls made for one case.
fn run(case: &Value) -> (Value, Value) {
    let git = Counted::new(case.get("s").unwrap_or(&json!({})));
    let lit = LiteralReader::default();
    let cwd = cps("/w");
    let toplevel = case.get("top").and_then(Value::as_str).map(cps);
    let at = Place {
        cwd: &cwd,
        toplevel: toplevel.as_deref(),
    };
    let c = CountReads {
        git: &git,
        at,
        literal_pathspecs: &literal,
        lit: &lit,
    };
    let cl = case.get("c").map(claims).unwrap_or_default();
    let a = case.get("a").map(texts).unwrap_or_default();
    let o = match case["k"].as_str().expect("k") {
        "anchors" => {
            let home = cps(HOME);
            let vp = paths(&case["vp"], case["kind"].as_str().expect("kind"));
            let mut lookup = |_: &[u32]| UserHome::Unknown;
            let got =
                commit_drift_anchor_paths(&text(&case["body"]), &vp, Some(&home), &mut lookup);
            outs(&got.expect("an answer"))
        }
        "count" => {
            let since = DateTime::from_isoformat(&text(&case["since"])).expect("a stamp");
            let w = walk(&case["w"]);
            let u = case["u"].as_i64().expect("u");
            match resolve_commit_drift_count(c, &since, u, &a, &cl, w.as_ref()) {
                Ok(n) => json!(n),
                Err(e) => error(CountError::Weak(e)),
            }
        }
        "specs" => anchor_specs(&git, at, &a, &cl).map_or(Value::Null, |s| outs(&s)),
        "quiet" => json!(quiescent_drift_applicable(c, &a, &cl)),
        "unread" => {
            let why = HistoryUnread::from_value(case["why"].as_str().expect("why")).expect("a why");
            applicable_unread(why, &git, at, &a, &cl).map_or(Value::Null, |w| json!(w.value()))
        }
        _ => ccd(case, &git),
    };
    assert_eq!(lit.gap(), None);
    let calls = git.script.calls.borrow().clone();
    (o, Value::Array(calls))
}

#[test]
fn golden_verify_count() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    let cases = &lines[1..];
    assert_eq!(header["count"], json!(cases.len()));
    let mut ledger = Ledger::new("golden_verify_count", FILE);
    for (n, case) in cases.iter().enumerate() {
        let label = format!("case {n} ({})", case["k"]);
        let (o, calls) = run(case);
        let want_calls = case.get("calls").cloned().unwrap_or(json!([]));
        let same = o == case["o"] && calls == want_calls;
        assert!(
            !ledger.differs_fatally(&label, same),
            "{label} differs: got {o} calls {calls}, want {case}"
        );
    }
    ledger.finish(cases.len());
}
