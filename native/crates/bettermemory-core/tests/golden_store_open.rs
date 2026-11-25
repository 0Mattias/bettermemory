//! The native writing open against `store_open.jsonl`: for every case,
//! the same starting store (a base of `store_bases.json` rebuilt, or the
//! store `store_create.json` pins, then the case's recipe), the native
//! open and close (twice where the case opens twice), and then:
//!
//! - `same`: every compared part equals what the reference's open left
//!   (all tables as one hash, `log` and the digest's tables on their own,
//!   meta and `digest_state` row by row, `sqlite_master`, the pragmas, the
//!   store and keys directories' names, the key ring's texts, the modes);
//! - `refused`: the reference's class and text, with the parts the
//!   reference's refusal changed changed (`reference_changed`: the modes
//!   `_connect` sets before meta is read);
//! - `not_ported:<state>`: refused as that state, with every compared part
//!   as it was before (nothing written; the reference's own writes are in
//!   the fixture as the parts it changed), or, beside a hot journal, as
//!   the journal's rollback alone leaves it (`rolled_back`);
//! - `raises`: the reference's class and text, raised after the same
//!   writes (every compared part as the reference's open left it).
//!
//! POSIX only: the fixture is (the binding is a resolved path, modes are
//! read); `store_open_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;

use bettermemory_core::store::open::{open_for_writing, OpenError};
use common::opened::{self, differing, scrub, state};
use common::parity::Ledger;
use common::stores::scratch;
use serde_json::{json, Value};

fn run(case: &Value, header: &Value) -> Result<(), String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let dir = scratch("open");
    let root = dir.0.clone();
    let c = root.join("c");
    opened::made(case["base"].as_str().expect("base"), &c);
    opened::setup(case["recipe"].as_array().expect("recipe"), &c, store_id);
    let before = state(&c, &root, store_id, pragmas);
    for part in ["master", "tables"] {
        if before[part] != case["before"][part] {
            return Err(format!(
                "{name}: the rebuilt store's {part} is not the reference's"
            ));
        }
    }
    let provider = opened::provider(header);
    let opens = case.get("opens").and_then(Value::as_u64).unwrap_or(1);
    let mut outcome: Result<(), OpenError> = Ok(());
    let mut synchronous: Option<i64> = None;
    for _ in 0..opens {
        match open_for_writing(&c.join("memory.sqlite"), &c.join("keys"), &provider) {
            Ok(store) => {
                synchronous = Some(
                    store
                        .connection()
                        .query_row("PRAGMA synchronous", [], |r| r.get(0))
                        .expect("synchronous"),
                );
                store.close().expect("close")
            }
            Err(e) => {
                outcome = Err(e);
                break;
            }
        }
    }
    let after = state(&c, &root, store_id, pragmas);
    let native = case["native"].as_str().expect("native");
    if native == "same" && outcome.is_ok() {
        let want = case["outcome"]["synchronous"].as_i64();
        if want != synchronous {
            return Err(format!(
                "{name}: the writing connection's synchronous is {synchronous:?}, the reference's {want:?}"
            ));
        }
    }
    match (native, &outcome) {
        ("same", Ok(())) => {
            let want = &case["after"];
            let parts = differing(want, &after);
            if !parts.is_empty() {
                return Err(format!(
                    "{name}: differs in {parts:?}\nwant {want}\ngot  {after}"
                ));
            }
        }
        ("refused", Err(e)) => {
            let want = &case["outcome"]["refusal"];
            let got_text = scrub(&e.to_string(), &root);
            if e.class() != want["class"] || got_text != want["text"] {
                return Err(format!(
                    "{name}: refused {} {got_text:?}, the reference {want}",
                    e.class()
                ));
            }
            // `_connect`'s modes, set before meta is read: what the
            // reference's refusal changed (`reference_changed`).
            let parts = differing(&before, &after);
            let want = case.get("reference_changed").cloned().unwrap_or(json!([]));
            if json!(parts) != want {
                return Err(format!(
                    "{name}: a refusal wrote {parts:?}, the reference {want}"
                ));
            }
        }
        ("raises", Err(e)) => {
            // Raised after the open's first writes, as the reference's.
            let want = &case["outcome"]["refusal"];
            let got_text = scrub(&e.to_string(), &root);
            if e.class() != want["class"] || got_text != want["text"] {
                return Err(format!(
                    "{name}: raised {} {got_text:?}, the reference {want}",
                    e.class()
                ));
            }
            let parts = differing(&case["after"], &after);
            if !parts.is_empty() {
                return Err(format!("{name}: differs in {parts:?}"));
            }
        }
        (n, Err(OpenError::NotPorted(state)))
            if n.strip_prefix("not_ported:") == Some(state.name()) =>
        {
            // Beside a hot journal, its rollback (what any connection that
            // can write does at its first read), and nothing more.
            let want = case.get("rolled_back").unwrap_or(&before);
            let parts = differing(want, &after);
            if !parts.is_empty() {
                return Err(format!("{name}: a not-ported refusal wrote {parts:?}"));
            }
        }
        (n, got) => {
            return Err(format!(
                "{name}: expected {n}, got {:?}",
                got.as_ref().map_err(|e| format!("{} {e}", e.class()))
            ))
        }
    }
    Ok(())
}

#[test]
fn every_case_opens_or_refuses_as_the_reference() {
    let (header, cases) = opened::cases();
    let mut failures = Vec::new();
    for case in &cases {
        if let Err(e) = run(case, &header) {
            failures.push(e);
        }
    }
    let ledger = Ledger::new(
        "every_case_opens_or_refuses_as_the_reference",
        "store_open.jsonl",
    );
    ledger.settle(cases.len(), &failures);
}
