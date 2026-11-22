//! Every case of `membership_migrate_report.jsonl` (not gated) and
//! `membership_migrate_report_python.jsonl` (gated on the Python release)
//! against the native `membership_migrate` report and helpers. The
//! spelling is the generator's (`tests/contract/gen_membership_migrate_report.py`).

mod common;

use bettermemory_core::membership_migrate::{
    self as mm, LegacyQuestion, MigrationReport, MigrationTotals, RootDecision, RootlessHome,
};
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::unicode;
use common::parity::Ledger;
use serde_json::Value;
use sha2::{Digest, Sha256};

fn rows(name: &str) -> Vec<Value> {
    let text = std::fs::read_to_string(common::golden_path(name))
        .unwrap_or_else(|e| panic!("cannot read golden file {name}: {e}"));
    text.lines()
        .map(|l| serde_json::from_str(l).expect("a JSON line"))
        .collect()
}

/// A value as the generator writes it: `neutral`, or a `rep` recipe.
fn value(v: &Value) -> PyValue {
    if v["t"] == "rep" {
        let mut out = Vec::new();
        for run in v["v"].as_array().expect("runs") {
            let piece = unicode::cps(run[0].as_str().expect("a piece"));
            for _ in 0..run[1].as_u64().expect("a count") {
                out.extend_from_slice(&piece);
            }
        }
        return PyValue::Str(out);
    }
    common::neutral(v)
}

fn text(v: &Value) -> PyStr {
    match &value(v) {
        PyValue::Str(s) => s.clone(),
        other => panic!("not a text: {other:?}"),
    }
}

fn int(v: &PyValue) -> i64 {
    match v {
        PyValue::Int(t) => t.parse().expect("an int"),
        other => panic!("not an int: {other:?}"),
    }
}

fn st(v: &PyValue) -> PyStr {
    match v {
        PyValue::Str(s) => s.clone(),
        other => panic!("not a str: {other:?}"),
    }
}

fn opt(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::None => None,
        other => Some(st(other)),
    }
}

fn items(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(x) | PyValue::Tuple(x) => x,
        other => panic!("not a sequence: {other:?}"),
    }
}

fn mapping(v: &PyValue) -> Vec<(PyStr, i64)> {
    match v {
        PyValue::Dict(d) => d.iter().map(|(k, v)| (st(k), int(v))).collect(),
        other => panic!("not a dict: {other:?}"),
    }
}

fn f<'a>(v: &'a PyValue, k: &str) -> &'a PyValue {
    v.get(k).unwrap_or_else(|| panic!("no field {k}"))
}

fn report(a: &PyValue) -> MigrationReport {
    let t = f(a, "totals");
    let ti = |k| int(f(t, k));
    MigrationReport {
        dry_run: matches!(f(a, "dry_run"), PyValue::Bool(true)),
        kept: st(f(a, "kept")),
        machine: st(f(a, "machine")),
        roots: items(f(a, "roots"))
            .iter()
            .map(|r| RootDecision {
                path: st(f(r, "path")),
                state: st(f(r, "state")),
                project: st(f(r, "project")),
                project_name: st(f(r, "project_name")),
                checkout: opt(f(r, "checkout")),
                rule: st(f(r, "rule")),
                memories: int(f(r, "memories")),
                episodes: int(f(r, "episodes")),
                tombstones: int(f(r, "tombstones")),
                recorded: int(f(r, "recorded")),
                held: mapping(f(r, "held")),
            })
            .collect(),
        rootless: items(f(a, "rootless"))
            .iter()
            .map(|r| RootlessHome {
                record: st(f(r, "record")),
                kind: st(f(r, "kind")),
                cwd: st(f(r, "cwd")),
                root: st(f(r, "root")),
                project: st(f(r, "project")),
                checkout: opt(f(r, "checkout")),
                rule: st(f(r, "rule")),
                carriers: items(f(r, "carriers")).iter().map(st).collect(),
            })
            .collect(),
        proposals: items(f(a, "proposals"))
            .iter()
            .map(|p| LegacyQuestion {
                proposal: st(f(p, "proposal")),
                checkout: st(f(p, "checkout")),
                at: st(f(p, "at")),
                legacy_project: st(f(p, "legacy_project")),
                root: st(f(p, "root")),
                line: st(f(p, "line")),
            })
            .collect(),
        totals: MigrationTotals {
            projects: ti("projects"),
            checkouts: ti("checkouts"),
            views: ti("views"),
            memory_homes: ti("memory_homes"),
            tombstone_homes: ti("tombstone_homes"),
            episode_homes: ti("episode_homes"),
            witnesses: ti("witnesses"),
            proposals: ti("proposals"),
            log_rows: ti("log_rows"),
            memories_rewritten: ti("memories_rewritten"),
            tombstones_rewritten: ti("tombstones_rewritten"),
            episodes_rewritten: ti("episodes_rewritten"),
        },
        homes_by_rule: mapping(f(a, "homes_by_rule")),
        remint: items(f(a, "remint")).iter().map(st).collect(),
    }
}

/// Equality with a NaN equal to a NaN (the reference's float `nan`).
fn eq(a: &PyValue, b: &PyValue) -> bool {
    match (a, b) {
        (PyValue::Float(x), PyValue::Float(y)) => x.to_bits() == y.to_bits(),
        (PyValue::List(x), PyValue::List(y)) | (PyValue::Tuple(x), PyValue::Tuple(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| eq(p, q))
        }
        (PyValue::Dict(x), PyValue::Dict(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|((k, v), (l, w))| eq(k, l) && eq(v, w))
        }
        _ => a == b,
    }
}

/// Whether `got` is the answer `o` records, a `chain` walked without
/// recursion.
fn same(got: &PyValue, o: &Value) -> bool {
    if o["t"] != "chain" {
        return eq(got, &common::neutral(o));
    }
    let mut v = got;
    for run in o["kinds"].as_array().expect("runs") {
        let kind = run[0].as_str().expect("a kind");
        for _ in 0..run[1].as_u64().expect("a count") {
            v = match (kind, v) {
                ("l", PyValue::List(x)) if x.len() == 1 => &x[0],
                (k, PyValue::Dict(d)) if d.len() == 1 && d[0].0 == PyValue::str(k) => &d[0].1,
                _ => return false,
            };
        }
    }
    eq(v, &common::neutral(&o["leaf"]))
}

fn answer(got: Result<PyValue, PyError>, o: &Value) -> bool {
    match got {
        Err(e) => o["raised"] == e.name(),
        Ok(v) => o.get("raised").is_none() && same(&v, o),
    }
}

fn check(row: &Value) -> Result<(), String> {
    let a = &row["a"];
    let ok = match row["f"].as_str().expect("f") {
        "constants" => {
            let tables: Vec<PyValue> = mm::RECORD_TABLES
                .iter()
                .map(|(t, k)| PyValue::List(vec![PyValue::str(t), PyValue::str(k)]))
                .collect();
            let got = PyValue::Dict(vec![
                (PyValue::str("LIVE"), PyValue::str(mm::LIVE)),
                (PyValue::str("GONE"), PyValue::str(mm::GONE)),
                (PyValue::str("REPLACED"), PyValue::str(mm::REPLACED)),
                (PyValue::str("UNREACHABLE"), PyValue::str(mm::UNREACHABLE)),
                (PyValue::str("RULE_PREFIX"), PyValue::str(mm::RULE_PREFIX)),
                (
                    PyValue::str("LEGACY_REASON"),
                    PyValue::str(mm::LEGACY_REASON),
                ),
                (PyValue::str("MIGRATED"), PyValue::str(mm::MIGRATED)),
                (PyValue::str("_RECORD_TABLES"), PyValue::List(tables)),
                (
                    PyValue::str("_RECORD_KINDS"),
                    PyValue::List(mm::RECORD_KINDS.iter().map(|k| PyValue::str(k)).collect()),
                ),
                (
                    PyValue::str("_PROBE_BUDGET"),
                    PyValue::Float(mm::PROBE_BUDGET),
                ),
                (
                    PyValue::str("_PROBE_WORKERS"),
                    PyValue::int(mm::PROBE_WORKERS as i128),
                ),
            ]);
            got == common::neutral(&row["o"])
        }
        "basename" => {
            let p = text(&a[0]);
            let host = if mm::HOST_NT {
                &row["nt"]
            } else {
                &row["posix"]
            };
            mm::basename_on(&p, false) == text(&row["posix"])
                && mm::basename_on(&p, true) == text(&row["nt"])
                && mm::basename(&p) == text(host)
        }
        "origin" => answer(
            mm::origin(&value(&a[0])).map(|o| o.map_or(PyValue::None, PyValue::Dict)),
            &row["o"],
        ),
        "json_list" => answer(mm::json_list(&value(&a[0])).map(PyValue::List), &row["o"]),
        "text" => {
            let o = value(&a[0]);
            let d = match &o {
                PyValue::None => None,
                PyValue::Dict(d) => Some(&d[..]),
                other => panic!("not an origin: {other:?}"),
            };
            mm::text(d, &text(&a[1])).map_or(PyValue::None, PyValue::Str)
                == common::neutral(&row["o"])
        }
        "id_order" => {
            let (n, id) = mm::id_order(&text(&a[0]));
            PyValue::List(vec![PyValue::int(n as i128), PyValue::Str(id)])
                == common::neutral(&row["o"])
        }
        "repository_name" => mm::repository_name(&text(&a[0])) == text(&row["o"]),
        "shas" => {
            let got = mm::shas(items(&value(&a[0])));
            PyValue::List(got.into_iter().map(PyValue::Str).collect()) == common::neutral(&row["o"])
        }
        "shas_sweep" => {
            let alphabet = text(&row["alphabet"]);
            let mut x: u64 = 1;
            let mut draw = || {
                x = x
                    .wrapping_mul(6364136223846793005)
                    .wrapping_add(1442695040888963407);
                (x >> 33) as usize
            };
            let n = row["n"].as_u64().expect("a count") as usize;
            let mut digest = Sha256::new();
            for _ in 0..n {
                let len = draw() % 71;
                let t: PyStr = (0..len)
                    .map(|_| alphabet[draw() % alphabet.len()])
                    .collect();
                let got = mm::shas(&[PyValue::Str(t)]);
                let line = got
                    .iter()
                    .next()
                    .map_or("-".to_string(), |g| unicode::to_string(g).expect("ascii"));
                digest.update(line.as_bytes());
                digest.update(b"\n");
            }
            let hex: String = digest
                .finalize()
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect();
            n > 0 && row["sha256"] == hex.as_str()
        }
        "report" => {
            let r = report(&value(a));
            let o = &row["o"];
            let lines = PyValue::List(r.lines().into_iter().map(PyValue::Str).collect());
            o["homed"].as_i64().map(i128::from) == Some(r.homed())
                && r.to_dict() == common::neutral(&o["to_dict"])
                && lines == common::neutral(&o["lines"])
        }
        other => panic!("unknown function {other}"),
    };
    if ok {
        Ok(())
    } else {
        Err(format!("{row}").chars().take(400).collect())
    }
}

fn run(test: &str, name: &str) {
    let rows = rows(name);
    let ledger = Ledger::new(test, name);
    let failures: Vec<String> = rows.iter().filter_map(|r| check(r).err()).collect();
    assert!(!rows.is_empty(), "{name}: no case");
    ledger.settle(rows.len(), &failures);
}

#[test]
fn membership_migrate_report_cases() {
    run(
        "membership_migrate_report_cases",
        "membership_migrate_report.jsonl",
    );
}

#[test]
fn membership_migrate_report_python_cases() {
    run(
        "membership_migrate_report_python_cases",
        "membership_migrate_report_python.jsonl",
    );
}
