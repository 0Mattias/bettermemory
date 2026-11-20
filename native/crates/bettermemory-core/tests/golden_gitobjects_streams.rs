//! Every case of `gitobjects_streams.jsonl` (class i) against
//! `gitobjects::stream`: a zlib stream read from a scripted handle call by
//! call, the delta chain walked over a pack, one entry inflated, a name
//! looked up in several indexes, and the sweeps, whose cases are made
//! here again from their seed and compared by the digest of their answers.

mod common;

use std::collections::BTreeSet;
use std::time::Duration;

use bettermemory_core::gitobjects::stream::{self, Link};
use bettermemory_core::gitobjects::PackIndex;
use common::gitobjects::{self as fx, File};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

fn script(case: &Value) -> Vec<i64> {
    case["s"]
        .as_array()
        .map(|s| s.iter().map(|k| k.as_i64().unwrap()).collect())
        .unwrap_or_default()
}

/// The case's handle over `data`: its script, its origin, its bad seek.
fn handle(case: &Value, data: Vec<u8>) -> File {
    let mut fh = File::new(data, script(case));
    fh.origin = case["org"].as_u64().unwrap_or(0);
    fh.bad_seek = case["ks"].as_u64();
    fh
}

/// A walk's answer as the fixture spells it.
fn walked(code: u8, links: &[Link]) -> Value {
    if links.len() > 8 {
        let text = fx::links_text(code, links);
        return json!({ "links": links.len(), "sha": fx::sha(text.as_bytes()) });
    }
    let spelled: Vec<Value> = links
        .iter()
        .map(|l| json!([l.start, l.size.hex(), l.target.as_ref().map(|t| t.hex())]))
        .collect();
    json!([code, spelled])
}

/// The native answer of a case, with the handle's log where the case
/// pins one.
fn answer(case: &Value, header: &Value) -> (Value, Option<Value>) {
    let packs = header["packs"].as_array().unwrap();
    let indexes = |case: &Value| -> Vec<PackIndex> {
        case["xs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|k| fx::index(&packs[k.as_u64().unwrap() as usize]))
            .collect()
    };
    match case["f"].as_str().unwrap() {
        "st" => {
            let mut fh = handle(case, fx::content(&case["d"]));
            let calls: Vec<(bool, u64)> = case["o"]
                .as_array()
                .unwrap()
                .iter()
                .map(|o| (o[0] == "e", o[1].as_u64().unwrap()))
                .collect();
            let chunk = case["c"].as_u64().unwrap() as usize;
            (Value::Array(fx::ops(&mut fh, chunk, &calls)), None)
        }
        "ch" => {
            let spec = match case["pk"].as_u64() {
                Some(k) => &packs[k as usize],
                None => case,
            };
            let mut fh = handle(case, fx::content(&spec["p"]));
            let got = stream::chain(
                &mut fh,
                &fx::index(spec),
                case["at"].as_u64().unwrap(),
                case["cap"].as_u64(),
            );
            let r = match got {
                Err(failure) => fx::raised(failure),
                Ok(None) => Value::Null,
                Ok(Some((code, links))) => walked(code, &links),
            };
            (r, Some(fh.logged()))
        }
        "ia" => {
            let mut fh = handle(case, fx::content(&case["p"]));
            let link = Link {
                start: case["l"][0].as_u64().unwrap(),
                size: fx::wide(case["l"][1].as_str().unwrap()),
                target: None,
            };
            let r = match stream::inflate_at(&mut fh, &link) {
                Err(failure) => fx::raised(failure),
                Ok(got) => fx::data(got.as_deref()),
            };
            (r, Some(fh.logged()))
        }
        "li" => {
            let chosen = indexes(case);
            let raw = common::unhex(case["raw"].as_str().unwrap());
            let r = match stream::listed_in(&chosen, &raw) {
                Err(r) => json!(["x", fx::raise_name(r)]),
                Ok(found) => found
                    .iter()
                    .map(|(pack, at)| {
                        let n = chosen.iter().position(|x| std::ptr::eq(x, *pack));
                        json!([n.unwrap(), at])
                    })
                    .collect(),
            };
            (r, None)
        }
        "tk" => {
            let chosen = indexes(case);
            let mut want: Vec<(Vec<u8>, String)> = case["want"]
                .as_array()
                .unwrap()
                .iter()
                .map(|w| {
                    let raw = common::unhex(w[0].as_str().unwrap());
                    (raw, w[1].as_str().unwrap().to_owned())
                })
                .collect();
            let mut found: BTreeSet<String> = case["found"]
                .as_array()
                .unwrap()
                .iter()
                .map(|f| f.as_str().unwrap().to_owned())
                .collect();
            let raised = stream::take(&chosen, &mut want, &mut found).err();
            let left: Vec<Value> = want
                .iter()
                .map(|(raw, sha)| json!([common::hex(raw), sha]))
                .collect();
            let mut r = json!({ "want": left, "found": found });
            if let Some(raise) = raised {
                r["x"] = json!(["x", fx::raise_name(raise)]);
            }
            (r, None)
        }
        "sw" => {
            let (seed, n) = (case["seed"].as_u64().unwrap(), case["n"].as_u64().unwrap());
            let sha = match case["rule"].as_str().unwrap() {
                "stream" => {
                    let bases: Vec<(Vec<u8>, u64)> = header["bases"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|b| (fx::content(&b[0]), b[1].as_u64().unwrap()))
                        .collect();
                    fx::sweep(seed, n, |rng| fx::sweep_stream(rng, &bases))
                }
                "chain" => {
                    let swept: Vec<(Vec<u64>, Vec<u8>, PackIndex)> = header["swept"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|k| {
                            let spec = &packs[k.as_u64().unwrap() as usize];
                            let offsets = fx::listed_offsets(spec);
                            (offsets, fx::content(&spec["p"]), fx::index(spec))
                        })
                        .collect();
                    fx::sweep(seed, n, |rng| fx::sweep_chain(rng, &swept))
                }
                other => panic!("a sweep by rule {other}"),
            };
            (json!(sha), None)
        }
        other => panic!("a case of kind {other}"),
    }
}

#[test]
fn gitobjects_streams_every_case() {
    // About two seconds in debug; a stream's loop that does not end fails
    // at the limit instead of hanging the run.
    fx::within(Duration::from_secs(120), every_case);
}

fn every_case() {
    let file = "gitobjects_streams.jsonl";
    let lines = jsonl(file);
    let (header, cases) = (&lines[0], &lines[1..]);
    assert_eq!(header["cases"].as_u64(), Some(cases.len() as u64));
    assert!(!cases.is_empty(), "{file}: no case");
    let ledger = Ledger::new("golden_gitobjects_streams", file);
    let mut failures = Vec::new();
    let mut seen = BTreeSet::new();
    let mut swept = 0;
    for (i, case) in cases.iter().enumerate() {
        let kind = case["f"].as_str().unwrap();
        seen.insert(kind.to_owned());
        let (got, log) = answer(case, header);
        let want = if kind == "sw" {
            swept += case["n"].as_u64().unwrap();
            &case["sha"]
        } else {
            &case["r"]
        };
        let mut shown = case.to_string();
        shown.truncate(300);
        if got != *want {
            failures.push(format!("#{i} {shown}: native {got}"));
        } else if log.as_ref().is_some_and(|log| *log != case["log"]) {
            failures.push(format!("#{i} {shown}: native log {}", log.unwrap()));
        }
    }
    assert_eq!(seen.len(), 6, "{file}: every kind of case is present");
    assert!(swept >= 20_000, "{file}: the sweeps hold {swept} cases");
    ledger.settle(cases.len(), &failures);
}
