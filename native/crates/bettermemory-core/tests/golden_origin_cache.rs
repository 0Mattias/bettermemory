//! Clock boundaries, signature equality, replacement, eviction and clearing.
mod common;

use bettermemory_core::origin_cache::OriginCache;
mod origin_cache_cases;
use bettermemory_core::unicode::{cps, to_string};
use common::parity::Ledger;
use origin_cache_cases::entry;
use serde_json::{json, Value};

#[test]
fn clock_and_cache_rows() {
    let file = "origin_cache.jsonl";
    let lines = common::jsonl(file);
    let mut ledger = Ledger::new("golden_origin_cache", file);
    let mut cache = OriginCache::default();
    for (i, row) in lines[1..].iter().enumerate() {
        ledger.case(i, || {
            if row[0] == "age" {
                let saved = entry("saved");
                let mut sig = saved.signature.clone();
                if row[3] == false {
                    sig.base.git_dir = Some(b"different".to_vec());
                }
                assert_eq!(
                    saved.matches(row[1].as_f64().unwrap(), row[2].as_f64().unwrap(), &sig),
                    row[4].as_bool().unwrap()
                );
            } else {
                let op = &row[1];
                let answer = match op[0].as_str().unwrap() {
                    "range" => {
                        for k in op[1].as_u64().unwrap()..op[2].as_u64().unwrap() {
                            cache.remember(cps(&k.to_string()), entry(&k.to_string()));
                        }
                        Value::Null
                    }
                    "clear" => {
                        cache.clear();
                        Value::Null
                    }
                    "write" => {
                        cache.remember(cps(&op[1].to_string()), entry(op[2].as_str().unwrap()));
                        Value::Null
                    }
                    "read" => cache
                        .lookup(&cps(&op[1].to_string()))
                        .map_or(Value::Null, |e| {
                            json!([
                                to_string(e.origin.origin.repo.as_ref().unwrap()),
                                to_string(&e.alternates[0])
                            ])
                        }),
                    other => panic!("unknown action {other}"),
                };
                assert_eq!(answer, row[2]);
            }
        });
    }
    ledger.finish(lines[0]["rows"].as_u64().unwrap() as usize);
}

#[cfg(not(unix))]
#[test]
fn directory_fixture_requires_posix_git_discovery() {
    common::parity::record(
        "golden_origin_cache_posix",
        "origin_cache_posix.jsonl",
        "os",
        0,
        &["each real repository scenario".to_owned()],
        &["the reference discovery checks POSIX ownership and symbolic links".to_owned()],
    );
}
