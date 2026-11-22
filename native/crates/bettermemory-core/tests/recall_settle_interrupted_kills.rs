//! Cases a plausible wrong port of `_probe_message` and
//! `settle_interrupted` fails, each an answer the reference gives
//! (`tests/contract/gen_recall_settle.py` pins the same shapes).

mod settle_int;

use bettermemory_core::pyvalue::PyValue;
use serde_json::json;
use settle_int::{cps, run};

fn ids(v: &[&str]) -> PyValue {
    PyValue::List(v.iter().map(|x| PyValue::Str(cps(x))).collect())
}

fn kinds(log: &[PyValue]) -> Vec<String> {
    log.iter()
        .map(|e| match e {
            PyValue::List(items) => match &items[0] {
                PyValue::Str(k) => k.iter().map(|&c| char::from_u32(c).unwrap()).collect(),
                _ => unreachable!(),
            },
            _ => unreachable!(),
        })
        .collect()
}

/// The audited set is read once: a prompt id twice in the transcript is
/// settled twice.
#[test]
fn a_repeated_prompt_id_settles_twice() {
    let (o, _) = run(
        "int",
        &json!({"turns": [["P1", "a", null, null], ["P1", "b", null, null]]}),
    );
    assert!(o == ids(&["P1", "P1"]));
}

/// `has_memories` is asked only for an empty pool.
#[test]
fn has_memories_only_for_an_empty_pool() {
    let (_, log) = run("probe", &json!({}));
    assert!(!kinds(&log).contains(&"hm".to_string()));
    let (_, log) = run("probe", &json!({"pool": [0], "hm": true}));
    assert_eq!(kinds(&log), ["pool", "hm", "probe"]);
}

/// The audited scan trusts the store's read: neither the kind nor the
/// stamp of a row is checked.
#[test]
fn the_audited_scan_filters_by_session_alone() {
    let ev = json!([{"kind": "other", "session": "S", "prompt_id": "P1"}]);
    let (o, _) = run(
        "int",
        &json!({"turns": [["P1", "a", null, null]], "ev": ev}),
    );
    assert!(o == ids(&[]));
}

/// No transcript path, an empty one or no file: nothing read, nothing
/// built.
#[test]
fn no_file_reads_nothing() {
    for case in [json!({"tp": null}), json!({"tp": ""})] {
        let (o, log) = run("int", &case);
        assert!(o == ids(&[]) && log.is_empty());
    }
    let (o, log) = run("int", &json!({"file": false}));
    assert!(o == ids(&[]));
    assert_eq!(kinds(&log), ["path"]);
}

/// The recorder is built before the transcript is read, even when no
/// turn settles.
#[test]
fn the_recorder_is_built_before_the_read() {
    let (_, log) = run("int", &json!({"turns": []}));
    assert_eq!(kinds(&log), ["path", "eok", "recorder", "turns"]);
}

/// A refused `triggered_from` raises after the probe, the earlier rows
/// kept.
#[test]
fn a_bad_trigger_raises_after_the_probe() {
    let (o, log) = run(
        "int",
        &json!({"turns": [["P1", "a", null, null]], "tf": "bogus"}),
    );
    assert!(matches!(o, PyValue::Dict(_)));
    assert_eq!(kinds(&log).last().unwrap(), "probe");
}

/// An empty search mode probes as hybrid, a turn with an empty reply is
/// one with a reply, and a previous audit bounds both reads.
#[test]
fn mode_reply_and_since() {
    let (_, log) = run(
        "int",
        &json!({"turns": [["P1", "a", "", null]], "sm": "", "pa": "2026-01-01T11:00:00+00:00"}),
    );
    let text = format!("{log:?}");
    assert!(text.contains(&format!("{:?}", cps("hybrid"))));
    let rec = log.last().unwrap();
    let PyValue::List(items) = rec else {
        unreachable!()
    };
    let PyValue::Dict(fields) = &items[2] else {
        unreachable!()
    };
    let present = fields
        .iter()
        .find(|(k, _)| *k == PyValue::Str(cps("assistant_present")))
        .unwrap();
    assert!(present.1 == PyValue::Bool(true));
}

/// Settlement probes with the floor; `_probe_message` passes the
/// caller's.
#[test]
fn the_settlement_keeps_the_floor() {
    let (_, log) = run("int", &json!({"turns": [["P1", "a", null, null]]}));
    let text = format!("{:?}", log);
    assert!(text.contains("Bool(true)"));
    let (_, log) = run("probe", &json!({"floor": false}));
    let PyValue::List(items) = &log[1] else {
        unreachable!()
    };
    let PyValue::Dict(kw) = &items[3] else {
        unreachable!()
    };
    let floor = kw
        .iter()
        .find(|(k, _)| *k == PyValue::Str(cps("floor")))
        .unwrap();
    assert!(floor.1 == PyValue::Bool(false));
}

/// `Path.is_file` is true for a regular file alone: a FIFO, a socket, a
/// device or a folder at the transcript path settles nothing.
#[test]
fn only_a_regular_file_is_read() {
    for node in ["fifo", "socket", "device", "dir"] {
        let (o, log) = run(
            "int",
            &json!({"node": node, "turns": [["P1", "a", null, null]]}),
        );
        assert!(o == ids(&[]), "{node}");
        assert_eq!(kinds(&log), ["path"], "{node}");
    }
}

/// A NUL in the expanded path is `ValueError` inside `is_file`: False,
/// nothing read.
#[test]
fn a_nul_in_the_path_settles_nothing() {
    let xp: Vec<u32> = "/t/ok.jsonl\0x".chars().map(|c| c as u32).collect();
    let (o, log) = run(
        "int",
        &json!({"xp": xp, "turns": [["P1", "a", null, null]]}),
    );
    assert!(o == ids(&[]));
    assert_eq!(kinds(&log), ["path"]);
}

/// A lone surrogate: POSIX's `os.fsencode` encodes it (surrogateescape
/// gives its byte back only for U+DC80..U+DCFF; U+D800 is a ValueError
/// there and is_file is False). The wide branch declines.
#[test]
fn a_lone_surrogate_settles_nothing_on_posix() {
    use bettermemory_core::recall_settle::interrupted::{host_bytes, Fault};
    let (o, log) = run(
        "int",
        &json!({"xp": [0x2F, 0xD800], "turns": [["P1", "a", null, null]]}),
    );
    if cfg!(windows) {
        assert!(o == settle_int::s("NotPorted"));
    } else {
        assert!(o == ids(&[]));
    }
    assert_eq!(kinds(&log), ["path"]);
    assert!(matches!(
        host_bytes::<()>(&[0x2F, 0xD800], true),
        Err(Fault::NotPorted)
    ));
    assert!(matches!(host_bytes::<()>(&[0x2F, 0xD800], false), Ok(None)));
    assert!(matches!(host_bytes::<()>(&[0x2F, 0xDCFF], false), Ok(Some(b)) if b == [0x2F, 0xFF]));
    assert!(
        matches!(host_bytes::<()>(&[0x2F, 0xE9], true), Ok(Some(b)) if b == "/\u{e9}".as_bytes())
    );
}

/// `Path.is_file` of 3.13 re-raises an OSError whose errno it does not
/// ignore: ENAMETOOLONG (OSError) and EACCES (PermissionError) raise;
/// ENOENT and ENOTDIR answer False.
#[test]
fn is_file_reraises_what_pathlib_does_not_ignore() {
    let long = format!("/t/{}", "a".repeat(300));
    let (o, log) = run("int", &json!({"tp": long}));
    assert!(o == settle_int::err("OSError", &settle_int::cps("ENAMETOOLONG")));
    assert_eq!(kinds(&log), ["path"]);
    let (o, _) = run("int", &json!({"locked": true}));
    assert!(o == settle_int::err("PermissionError", &settle_int::cps("EACCES")));
    let (o, _) = run("int", &json!({"tp": "/t/ok.jsonl/x"}));
    assert!(o == ids(&[]));
    let (o, _) = run("int", &json!({"tp": "/nope/ok.jsonl"}));
    assert!(o == ids(&[]));
}
