//! Every case of `attestation_paths.jsonl` (not gated) against
//! `attestation_paths`, over the scripted host `gen_attestation_paths`
//! describes: `expanduser` and `lstat` by rule, `os.name` the row's.

mod common;

use bettermemory_core::attestation_paths::{
    commit_paths, compile_targets, path_mode, record_paths, unverifiable_inputs, Kind, Mode, Paths,
    Record, Targets,
};
use bettermemory_core::pyvalue::PyStr;
use common::attestation_paths::{
    cps, per_input, raised, spell, spell_compiled, surrogatepass, sweep_inputs, text, Scripted,
    MODES, NAMED_ROOTS, ROOTS,
};
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn mode_of(name: &str) -> Mode {
    Mode::parse(name).unwrap()
}

fn mode_name(mode: Mode) -> &'static str {
    match mode {
        Mode::Auto => "auto",
        Mode::Literal => "literal",
        Mode::Reference => "reference",
    }
}

fn texts(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| cps(s.as_str().unwrap()))
        .collect()
}

fn cps_of(v: &Value) -> PyStr {
    v.as_array()
        .unwrap()
        .iter()
        .map(|c| c.as_u64().unwrap() as u32)
        .collect()
}

fn digest(answers: &[PyStr]) -> String {
    let mut h = Sha256::new();
    for a in answers {
        h.update(surrogatepass(a));
        h.update([0x1e]);
    }
    common::hex(&h.finalize())
}

fn check_one(r: &Value) {
    let host = Scripted {
        nt: r["nt"].as_bool().unwrap(),
    };
    let got: Vec<String> = per_input(&host, &cps_of(&r["s"]), &NAMED_ROOTS)
        .iter()
        .map(|a| text(a))
        .collect();
    assert_eq!(json!(got), r["o"]);
}

fn check_long(r: &Value) {
    let host = Scripted {
        nt: r["nt"].as_bool().unwrap(),
    };
    let s = &r["s"];
    let mut input = cps_of(&s[0]);
    for _ in 0..s[2].as_u64().unwrap() {
        input.extend(cps_of(&s[1]));
    }
    input.extend(cps_of(&s[3]));
    let answers = per_input(&host, &input, &NAMED_ROOTS);
    assert_eq!(answers.len() as u64, r["n"].as_u64().unwrap());
    assert_eq!(digest(&answers), r["sha"].as_str().unwrap());
}

fn targets_of(v: &Value) -> Option<Targets> {
    v.as_array().map(|a| {
        a.iter()
            .map(|p| (cps(p[0].as_str().unwrap()), p[1].as_str().map(cps)))
            .collect()
    })
}

fn paths_json(p: &Paths) -> Value {
    json!([
        mode_name(path_mode(p)),
        p.items.iter().map(|s| text(s)).collect::<Vec<_>>()
    ])
}

fn check_record(r: &Value) {
    let spec = &r["rec"];
    let record = Record {
        verified_paths: texts(&spec["vp"]),
        verified_absent_paths: texts(&spec["va"]),
        verified_path_targets: targets_of(&spec["vt"]),
        verified_absent_path_targets: targets_of(&spec["vat"]),
    };
    let mut calls = Vec::new();
    let uv = unverifiable_inputs(&record, &mut |paths: &Paths| {
        calls.push(paths_json(paths));
        paths
            .items
            .iter()
            .filter(|p| p.last() == Some(&u32::from(b'x')))
            .cloned()
            .collect()
    });
    let out = json!({
        "rp0": paths_json(&record_paths(&record, false)),
        "rp1": paths_json(&record_paths(&record, true)),
        "cp": match commit_paths(&record) {
            Ok(p) => paths_json(&p),
            Err(e) => json!(text(&raised(e))),
        },
        "uv": uv.iter().map(|s| text(s)).collect::<Vec<_>>(),
        "calls": calls,
    });
    assert_eq!(out, r["o"]);
}

fn check_mode(r: &Value) {
    let kind = match r["kind"].as_str().unwrap() {
        "L" => Kind::Literal,
        "R" => Kind::Reference,
        _ => Kind::Plain,
    };
    let paths = Paths {
        kind,
        items: vec![cps("a")],
    };
    assert_eq!(json!(mode_name(path_mode(&paths))), r["o"]);
}

fn check_badmode(r: &Value) {
    let got = spell(Mode::parse(r["mode"].as_str().unwrap()), |_| cps("ok"));
    assert_eq!(json!(text(&got)), r["o"]);
}

fn check_compile(r: &Value) {
    let host = Scripted {
        nt: r["nt"].as_bool().unwrap(),
    };
    let root = r["root"].as_str().map(cps);
    let got = compile_targets(
        &host,
        &texts(&r["v"]),
        root.as_deref(),
        mode_of(r["mode"].as_str().unwrap()),
        r["absent"].as_bool().unwrap(),
    );
    assert_eq!(json!(text(&spell_compiled(got))), r["o"]);
}

fn check_sweep(r: &Value) {
    let host = Scripted {
        nt: r["nt"].as_bool().unwrap(),
    };
    let inputs = sweep_inputs(
        r["seed"].as_u64().unwrap(),
        r["inputs"].as_u64().unwrap() as usize,
    );
    let mut answers = Vec::new();
    for (i, s) in inputs.iter().enumerate() {
        answers.extend(per_input(&host, s, &ROOTS));
        let t = &inputs[(i + 1) % inputs.len()];
        let values = [s.clone(), t.clone(), s.clone()];
        for root in ROOTS {
            let root = root.map(cps);
            for mode in MODES {
                for absent in [false, true] {
                    answers.push(spell_compiled(compile_targets(
                        &host,
                        &values,
                        root.as_deref(),
                        mode,
                        absent,
                    )));
                }
            }
        }
    }
    assert_eq!(answers.len() as u64, r["n"].as_u64().unwrap());
    assert_eq!(digest(&answers), r["sha"].as_str().unwrap());
}

#[test]
fn attestation_paths_every_case() {
    let file = "attestation_paths.jsonl";
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let mut ledger = Ledger::new("attestation_paths_every_case", file);
    for (i, r) in rows.iter().enumerate() {
        let label = format!("line {} {}", i + 2, r["k"]);
        ledger.case(label, || match r["k"].as_str() {
            Some("one") => check_one(r),
            Some("long") => check_long(r),
            Some("record") => check_record(r),
            Some("mode") => check_mode(r),
            Some("badmode") => check_badmode(r),
            Some("compile") => check_compile(r),
            Some("sweep") => check_sweep(r),
            other => panic!("unknown row kind {other:?}"),
        });
    }
    ledger.finish(rows.len());
}
