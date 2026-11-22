//! Every case of `transcript.jsonl` (not gated) and
//! `transcript_python.jsonl` (gated on the Python release) against
//! `transcript`'s first unit: `ReverseLines` over files and faults, `_row`,
//! and `stop_turn` with the calls it makes into `owner_prompt.py`
//! replayed and checked in order. The spelling is the generator's
//! (`tests/contract/gen_transcript.py`).

mod common;

use std::num::NonZeroUsize;

use bettermemory_core::fsdata::StdFs;
use bettermemory_core::transcript::{
    self as t, row, stop_turn, FsOpener, Opener, ReverseLines, StopTurn,
};
use common::transcript::{big_lines, runs, MemTranscript, Replay};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn nz(v: &Value) -> NonZeroUsize {
    NonZeroUsize::new(v.as_u64().unwrap() as usize).unwrap()
}

fn walk<O: Opener>(o: &O, r: &Value, data: &MemTranscript) -> (Vec<Vec<u8>>, bool) {
    let needle = r.get("n").map(|n| common::unhex(n.as_str().unwrap()));
    let mut lines = ReverseLines::new(
        o,
        b"t.jsonl",
        nz(&r["c"]),
        r["b"].as_i64().unwrap(),
        needle.as_deref(),
    );
    let mut got = Vec::new();
    let cut = r.get("x").filter(|x| x[0] == "cut");
    for line in lines.by_ref() {
        got.push(line);
        if let Some(cut) = cut {
            if got.len() as u64 == cut[1].as_u64().unwrap() {
                data.data
                    .borrow_mut()
                    .truncate(cut[2].as_u64().unwrap() as usize);
            }
        }
    }
    (got, lines.reached_start)
}

fn check_reverse(r: &Value) {
    let mut file = MemTranscript::new(runs(&r["f"]));
    match r.get("x") {
        Some(Value::String(s)) if s == "missing" => file.refuse = Some("missing"),
        Some(Value::String(s)) if s == "dir" => file.refuse = Some("dir"),
        Some(x) if x[0] == "big" => file.extra = x[1].as_u64().unwrap(),
        _ => {}
    }
    let (got, rs) = walk(&file, r, &file);
    let want: Vec<Vec<u8>> = r["l"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| common::unhex(h.as_str().unwrap()))
        .collect();
    assert_eq!(got, want, "lines of {r}");
    assert_eq!(json!(rs), r["rs"], "reached_start of {r}");
}

fn check_digest(r: &Value) {
    assert_eq!(r["rule"], "lines");
    let data = big_lines(r["count"].as_u64().unwrap() as usize);
    assert_eq!(json!(data.len()), r["size"]);
    let file = MemTranscript::new(data);
    let (got, rs) = walk(&file, r, &file);
    let mut sha = Sha256::new();
    for line in &got {
        sha.update(line);
        sha.update(b"\n");
    }
    assert_eq!(json!(got.len()), r["yielded"], "count of {}", r["c"]);
    assert_eq!(common::hex(&sha.finalize()), r["sha256"].as_str().unwrap());
    assert_eq!(json!(rs), r["rs"]);
}

fn check_row(r: &Value) {
    let line = runs(&r["line"]);
    let got = row(&line);
    match (&r["r"], got) {
        (Value::String(e), Err(err)) => assert_eq!(err.name(), e),
        (Value::Null, Ok(None)) => {}
        (want, Ok(Some(value))) if want.is_object() => {
            let want = common::neutral(want);
            assert_eq!(format!("{value:?}"), format!("{want:?}"), "row of {r}");
        }
        (want, got) => panic!("row {r}: want {want}, got {got:?}"),
    }
}

fn opt(v: &Value) -> Option<Vec<u32>> {
    if v.is_null() {
        None
    } else {
        Some(common::text_field(v, "v"))
    }
}

fn check_stop(r: &Value) {
    let file = MemTranscript::new(runs(&r["f"]));
    let replay = Replay::of(&r["q"]);
    let pid = r["p"].as_str().map(bettermemory_core::unicode::cps);
    let got = stop_turn(
        &file,
        &replay,
        b"t.jsonl",
        pid.as_deref(),
        nz(&r["c"]),
        r["b"].as_i64().unwrap(),
    );
    match (&r["r"], got) {
        (Value::String(e), Err(err)) => assert_eq!(err.name(), e),
        (Value::Null, Ok(None)) => {}
        (want, Ok(Some(turn))) if want.is_object() => assert_eq!(
            turn,
            StopTurn {
                prompt: opt(&want["prompt"]),
                reply: opt(&want["reply"]),
                model: opt(&want["model"]),
            },
            "stop_turn of {r}"
        ),
        (want, got) => panic!("stop_turn {r}: want {want}, got {got:?}"),
    }
    assert_eq!(replay.left(), 0, "calls the port did not make: {r}");
}

fn run(name: &str) {
    let lines = common::jsonl(name);
    let mut checked = 0;
    let mut cases = 0;
    for r in &lines {
        let kind = r["k"].as_str().unwrap();
        match kind {
            "header" => {
                assert_eq!(r["CHUNK_BYTES"], json!(t::CHUNK_BYTES));
                assert_eq!(r["WALK_BYTES"], json!(t::WALK_BYTES));
                assert_eq!(r["INTERRUPT_OPENING"], json!(t::INTERRUPT_OPENING));
                continue;
            }
            "rl" => check_reverse(r),
            "rld" => check_digest(r),
            "row" => check_row(r),
            "st" => check_stop(r),
            other => panic!("kind {other}"),
        }
        cases += 1;
        checked += 1;
    }
    common::assert_count(name, checked, cases);
}

#[test]
fn transcript_every_case() {
    run("transcript.jsonl");
}

#[test]
fn transcript_python_every_case() {
    run("transcript_python.jsonl");
}

/// The open through the crate's file system: a missing file and a folder
/// end the walk before any line, `reached_start` False; a file reads.
#[test]
fn transcript_fs_opener() {
    let scratch = std::env::temp_dir().join(format!("bm-transcript-{}", std::process::id()));
    std::fs::create_dir_all(&scratch).unwrap();
    let fs = StdFs;
    let opener = FsOpener(&fs);
    let file = scratch.join("t.jsonl");
    std::fs::write(&file, b"a\nbb\nccc").unwrap();
    let path = |p: &std::path::Path| p.to_str().unwrap().as_bytes().to_vec();
    let c = NonZeroUsize::new(2).unwrap();
    let mut lines = ReverseLines::new(&opener, &path(&file), c, 1 << 30, None);
    let got: Vec<Vec<u8>> = lines.by_ref().collect();
    assert_eq!(got, vec![b"ccc".to_vec(), b"bb".to_vec(), b"a".to_vec()]);
    assert!(lines.reached_start);
    for absent in [scratch.join("absent"), scratch.clone()] {
        let mut lines = ReverseLines::new(&opener, &path(&absent), c, 1 << 30, None);
        assert_eq!(lines.next(), None);
        assert!(!lines.reached_start);
    }
    std::fs::remove_file(&file).unwrap();
    let _ = std::fs::remove_dir(&scratch);
}
