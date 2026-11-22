//! Every case of `transcript_turns.jsonl` (not gated) against
//! `transcript`'s second unit: `interrupted_turns`, `owner_words` and
//! `compact_marker`, with the calls they make into `owner_prompt.py`
//! replayed and checked in order. The spelling is the generator's
//! (`tests/contract/gen_transcript.py`).

mod common;

use std::num::NonZeroUsize;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::fsdata::StdFs;
use bettermemory_core::transcript::{
    compact_marker, interrupted_turns, owner_words, FsOpener, Opener,
};
use bettermemory_core::unicode::cps;
use common::transcript::{runs, MemTranscript, Replay};
use serde_json::{json, Value};

fn nz(v: &Value) -> NonZeroUsize {
    NonZeroUsize::new(v.as_u64().unwrap() as usize).unwrap()
}

fn dt(v: &Value) -> DateTime {
    DateTime::from_isoformat(&cps(v.as_str().unwrap())).unwrap()
}

fn opt(v: &Value) -> Value {
    if v.is_null() {
        Value::Null
    } else {
        json!(common::text_field(v, "v"))
    }
}

fn cp(text: &Option<Vec<u32>>) -> Value {
    text.as_ref().map_or(Value::Null, |t| json!(t))
}

fn check_turns(r: &Value, file: &MemTranscript) {
    let replay = Replay::of(&r["q"]);
    let got = interrupted_turns(
        file,
        &replay,
        b"t.jsonl",
        &dt(&r["s"]),
        nz(&r["c"]),
        r["b"].as_i64().unwrap(),
    );
    match (&r["r"], got) {
        (Value::String(e), Err(err)) => assert_eq!(err.name(), e, "{r}"),
        (Value::Array(want), Ok(turns)) => {
            assert_eq!(turns.len(), want.len(), "turns of {r}");
            for (turn, w) in turns.iter().zip(want) {
                assert_eq!(cp(&turn.prompt_id), opt(&w["id"]), "{r}");
                assert_eq!(cp(&turn.prompt), opt(&w["prompt"]), "{r}");
                assert_eq!(cp(&turn.reply), opt(&w["reply"]), "{r}");
                assert_eq!(cp(&turn.model), opt(&w["model"]), "{r}");
                assert_eq!(json!(turn.at.isoformat()), w["at"], "{r}");
            }
        }
        (want, got) => panic!("interrupted_turns {r}: want {want}, got {got:?}"),
    }
    assert_eq!(replay.left(), 0, "calls the port did not make: {r}");
}

fn check_words(r: &Value, file: &mut MemTranscript) {
    if let Some(Value::String(x)) = r.get("x") {
        file.refuse = Some(if x == "missing" { "missing" } else { "dir" });
    }
    let replay = Replay::of(&r["q"]);
    let got = owner_words(
        &*file,
        &replay,
        b"t.jsonl",
        r["n"].as_i64().unwrap(),
        nz(&r["c"]),
        r["b"].as_i64().unwrap(),
    )
    .unwrap();
    assert_eq!(got, common::text_field(&r["r"], "v"), "owner_words of {r}");
    assert_eq!(replay.left(), 0, "calls the port did not make: {r}");
}

fn check_marker(r: &Value, file: &MemTranscript) {
    let got = compact_marker(
        file,
        b"t.jsonl",
        &dt(&r["near"]),
        i128::from(r["w"].as_i64().unwrap()),
        nz(&r["c"]),
        r["b"].as_i64().unwrap(),
    );
    match (&r["r"], got) {
        (Value::String(e), Err(err)) => assert_eq!(err.name(), e, "{r}"),
        (Value::Null, Ok(None)) => {}
        (want, Ok(Some(m))) if want.is_object() => {
            assert_eq!(m.key, common::text_field(want, "key"), "{r}");
            assert_eq!(json!(m.at.isoformat()), want["at"], "{r}");
        }
        (want, got) => panic!("compact_marker {r}: want {want}, got {got:?}"),
    }
}

#[test]
fn transcript_turns_every_case() {
    let name = "transcript_turns.jsonl";
    let lines = common::jsonl(name);
    let mut files: Vec<Vec<u8>> = Vec::new();
    let (mut checked, mut cases) = (0, 0);
    let mut kinds = [0usize; 3];
    for r in &lines {
        let kind = r["k"].as_str().unwrap();
        if kind == "file" {
            assert_eq!(r["fi"], json!(files.len()));
            files.push(runs(&r["f"]));
            continue;
        }
        cases += 1;
        let mut file = MemTranscript::new(files[r["fi"].as_u64().unwrap() as usize].clone());
        match kind {
            "it" => {
                check_turns(r, &file);
                kinds[0] += 1;
            }
            "ow" => {
                check_words(r, &mut file);
                kinds[1] += 1;
            }
            "cm" => {
                check_marker(r, &file);
                kinds[2] += 1;
            }
            other => panic!("kind {other}"),
        }
        checked += 1;
    }
    assert!(
        kinds.iter().all(|&k| k > 0),
        "a kind with no case: {kinds:?}"
    );
    common::assert_count(name, checked, cases);
}

/// `path.is_file()` through the crate's file system: a regular file is
/// one; a missing path and a folder are not, so `owner_words` answers ""
/// before any open.
#[test]
fn transcript_turns_is_file() {
    let scratch = std::env::temp_dir().join(format!("bm-transturns-{}", std::process::id()));
    std::fs::create_dir_all(&scratch).unwrap();
    let fs = StdFs;
    let opener = FsOpener(&fs);
    let file = scratch.join("t.jsonl");
    std::fs::write(&file, b"x").unwrap();
    let path = |p: &std::path::Path| p.to_str().unwrap().as_bytes().to_vec();
    assert!(opener.is_file(&path(&file)));
    assert!(!opener.is_file(&path(&scratch.join("absent"))));
    assert!(!opener.is_file(&path(&scratch)));
    std::fs::remove_file(&file).unwrap();
    let _ = std::fs::remove_dir(&scratch);
}
