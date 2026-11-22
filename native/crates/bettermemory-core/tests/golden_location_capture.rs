//! Every case of `location_capture.jsonl` (not gated) and
//! `location_capture_platform.jsonl` (gated on the platform: `mode_t`'s
//! width) against `location::capture`, each call outside the unit served
//! by the generator's scripted world and its log compared in order.

mod common;
mod location_capture_world;

use bettermemory_core::location::capture::{
    token_fields, walk_files, AboveCache, Declined, MODE_T_BITS,
};
use common::parity::Ledger;
use location_capture_world::{cps, kv, run_capture, st, strs, World, P, S};
use serde_json::{json, Value};

fn walk_row(row: &Value) -> (Vec<String>, Value) {
    let world = World::new(&json!({"keys": {"/a": ["1", "1", null]}}));
    let cache = AboveCache::new(row["cap"].as_u64().unwrap() as usize);
    let mut out = Vec::new();
    for call in row["calls"].as_array().unwrap() {
        {
            let mut spec = world.spec.borrow_mut();
            spec["above_start"] = call[2].clone();
            spec["walk"] = json!({"files": [["bmp_9", "n", "/a", false], ["bmp_8", "m", "/z", true]], "errors": ["e"]});
        }
        *world.clocks.borrow_mut() = vec![call[3].clone()];
        let root = call[1].as_str().map(cps);
        let (walk, seen, errors) = walk_files(
            &mut P(&world),
            &cache,
            &mut || world.clock(),
            &mut |p| {
                use bettermemory_core::location::capture::Steps;
                S(&world).object_key(p, None)
            },
            &cps(call[0].as_str().unwrap()),
            root.as_deref(),
        )
        .unwrap();
        let files: Vec<Value> = seen
            .iter()
            .map(|f| {
                json!([
                    st(&f.id),
                    st(&f.name),
                    st(&f.dir),
                    st(&f.path),
                    f.boundary,
                    kv(&f.key)
                ])
            })
            .collect();
        let keys: Vec<String> = cache.keys().iter().map(|k| st(k)).collect();
        out.push(json!({
            "above": walk.above,
            "files": files,
            "errors": errors.iter().map(|e| st(e)).collect::<Vec<_>>(),
            "cache": keys,
        }));
    }
    let log = world.log.borrow().clone();
    (log, Value::Array(out))
}

fn token_row(row: &Value) -> (Vec<String>, Value) {
    let world = World::new(&json!({}));
    world.note(&["token_read", "/g"]);
    let read = (
        row["read"][0].as_str().unwrap().to_string(),
        row["read"][1].as_str().map(cps),
    );
    let bare = if row["bare"].as_bool().unwrap() {
        "True"
    } else {
        "False"
    };
    let got = token_fields(read, row["write_token"].as_bool().unwrap(), &mut || {
        world.note(&["token_write", "/g", "True", bare]);
        Ok::<_, Declined>((
            row["write"][0].as_str().unwrap().to_string(),
            row["write"][1].as_str().map(cps),
        ))
    })
    .unwrap();
    let s = |t: &Option<Vec<u32>>| t.as_ref().map_or(Value::Null, |t| json!(st(t)));
    let log = world.log.borrow().clone();
    (log, json!([s(&got.0), s(&got.1), st(&got.2)]))
}

fn run(name: &str) {
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("golden_location_capture", name);
    for (i, row) in rows.iter().enumerate().skip(1) {
        ledger.case(format!("{name}:{i}"), || {
            let (log, out) = match row["kind"].as_str().unwrap() {
                "capture" => run_capture(row, MODE_T_BITS),
                "walk" => walk_row(row),
                "token" => token_row(row),
                other => panic!("unknown kind {other}"),
            };
            let want: Vec<String> = strs(&row["log"]).iter().map(|l| st(l)).collect();
            assert_eq!(log, want, "{name} row {i} log");
            assert_eq!(out, row["out"], "{name} row {i}: {row}");
        });
    }
    assert!(rows.len() > 1, "{name} holds no case");
    assert_eq!(rows[0]["cases"].as_u64(), Some(rows.len() as u64 - 1));
    ledger.finish(rows.len() - 1);
}

#[test]
fn location_capture_every_case() {
    run("location_capture.jsonl");
}

#[test]
fn location_capture_platform_every_case() {
    run("location_capture_platform.jsonl");
}
