//! The store's document frequency source against
//! `store_docfreq_store.jsonl`, `store_docfreq_store_b.jsonl` and
//! `store_docfreq_advance.jsonl`: every case opened natively from the store
//! `store_create.json` pins (each table's columns checked against the
//! file's header, since a step's rows are laid by position), then each
//! step's tables laid into the file,
//! and each question asked through `store::docfreq_store` on the
//! connection the step names (the owner's; a transaction of its own, one
//! whose connection rolled back; `query_only`; a second connection holding
//! the write lock; a read-only connection under the path or another
//! spelling of it; one left inside a transaction no call owns), with one
//! memo per case. A transaction that commits in the reference commits
//! here. Compared exactly: every answer (the digest's own `mark`, `rows`
//! and `head` among them), the memo's keys and terms kept, each memo
//! entry's inputs (an entry carried over a moved head by
//! `_advanced_inputs` among them), the digest's state and rows after, the
//! head checkpoint unchanged, and the warnings with their classes, codes
//! and messages. POSIX only, as the fixture is;
//! `golden_store_docfreq_store_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;

use bettermemory_core::store::docfreq::DocFreqMemo;
use bettermemory_core::store::docfreq_store::StoreDocFreq;
use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::store::reads::set_busy_timeout;
use common::docfreq_store::{
    answers, columns_checked, digest_after, entries_as_keyed_here, entries_view, file_cases, files,
    head_bytes, lay_step, memo_view, want_warnings, warnings_view, FILES,
};
use common::opened;
use common::parity::Ledger;
use common::stores::scratch;
use common::txs::Replay;
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};

fn run(case: &Value, header: &Value) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let dir = scratch("dfs");
    let root = dir.0.clone();
    let (file, keys) = files(&root);
    opened::made("created", file.parent().expect("dir"));
    let provider = opened::provider(header);
    let mut store = open_for_writing(&file, &keys, &provider)
        .map_err(|e| format!("{name}: the open refused: {e}"))?;
    columns_checked(&file, header).map_err(|e| format!("{name}: the opened tables: {e}"))?;
    let memo = DocFreqMemo::new();
    let mut checked = 0;
    for step in case["steps"].as_array().expect("steps") {
        let label = format!("{name} step {}", step["step"]);
        lay_step(&file, step);
        let before = head_bytes(&keys, store_id);
        let asks = &step["answers"];
        let conn = step["conn"].as_str().expect("conn");
        let (got, warnings) = match conn {
            "plain" | "query_only" | "busy" | "residue" => {
                let mut other = None;
                if conn == "residue" {
                    store.connection().execute_batch("BEGIN").expect("begin");
                }
                if conn == "query_only" {
                    store
                        .connection()
                        .execute_batch("PRAGMA query_only = ON")
                        .expect("on");
                }
                if conn == "busy" {
                    let o = Connection::open(&file).expect("other");
                    o.execute_batch("BEGIN IMMEDIATE").expect("lock");
                    other = Some(o);
                    set_busy_timeout(store.connection(), 0.0).expect("timeout");
                }
                let src = StoreDocFreq::of_store(&store);
                let got = answers(&src, &memo, asks);
                let warnings = src.warnings();
                drop(src);
                if !store.connection().is_autocommit() {
                    store
                        .connection()
                        .execute_batch("ROLLBACK")
                        .expect("rollback");
                }
                if conn == "query_only" {
                    store
                        .connection()
                        .execute_batch("PRAGMA query_only = OFF")
                        .expect("off");
                }
                if let Some(o) = other {
                    o.execute_batch("ROLLBACK").expect("release");
                    set_busy_timeout(store.connection(), 5.0).expect("timeout");
                }
                (got, warnings)
            }
            "read_only" | "spelled" => {
                let path = if conn == "spelled" {
                    root.join("c").join("..").join("c").join("memory.sqlite")
                } else {
                    file.clone()
                };
                let ro = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_ONLY)
                    .expect("read-only");
                let src = StoreDocFreq::over(&ro, &path, store_id);
                let got = answers(&src, &memo, asks);
                (got, src.warnings())
            }
            "batch" | "batch_failed" | "batch_rolled" | "batch_raised" => {
                let path = store.path().to_owned();
                let mut clocks = Replay::new(&[], &[]);
                let tx = store
                    .begin(&mut clocks)
                    .map_err(|e| format!("{label}: {e}"))?;
                if conn == "batch_rolled" {
                    tx.connection().execute_batch("ROLLBACK").expect("rollback");
                }
                let src = StoreDocFreq::of_transaction(&tx, &path, store_id);
                let got = answers(&src, &memo, asks);
                let warnings = src.warnings();
                drop(src);
                // The reference's batch commits what it joined, unless it
                // raised or SQLite rolled it back (its commit then fails).
                if conn == "batch" || conn == "batch_failed" {
                    tx.commit().map_err(|e| format!("{label}: {e}"))?;
                }
                (got, warnings)
            }
            other => panic!("a connection {other}"),
        };
        let after = head_bytes(&keys, store_id);
        let mut differs = Vec::new();
        if got != *asks {
            differs.push(format!("answers {got} want {asks}"));
        }
        let memo_got = memo_view(&memo, &root);
        if memo_got != step["memo"] {
            differs.push(format!("memo {memo_got} want {}", step["memo"]));
        }
        // Each entry's inputs, field for field (an entry carried over a
        // moved head among them); a group key's bytes client or model is
        // its text here, the one stated difference.
        let entries = entries_view(&memo);
        let want_entries = entries_as_keyed_here(&step["entries"]);
        if entries != want_entries {
            differs.push(format!("entries {entries} want {want_entries}"));
        }
        let digest = digest_after(&file);
        if digest != step["digest"] {
            differs.push(format!("digest {digest} want {}", step["digest"]));
        }
        if !step["head_unchanged"].is_null() && json!(before == after) != step["head_unchanged"] {
            differs.push("head".to_owned());
        }
        let (w, want) = (
            warnings_view(&warnings, &root),
            want_warnings(&step["warnings"]),
        );
        if w != want {
            differs.push(format!("warnings {w} want {want}"));
        }
        if !differs.is_empty() {
            return Err(format!("{label}: {}", differs.join("; ")));
        }
        checked += asks.as_array().expect("asks").len();
    }
    Ok(checked)
}

fn check(test: &str, file: &str) {
    let (header, cases) = file_cases(file);
    let mut failures = Vec::new();
    let mut checked = 0;
    for case in &cases {
        match run(case, &header) {
            Ok(n) => checked += n,
            Err(e) => failures.push(e),
        }
    }
    assert!(checked > 0 || !failures.is_empty(), "no question checked");
    let want: usize = cases
        .iter()
        .flat_map(|c| c["steps"].as_array().expect("steps"))
        .map(|s| s["answers"].as_array().expect("answers").len())
        .sum();
    assert!(
        !failures.is_empty() || checked == want,
        "checked {checked} of {want}"
    );
    eprintln!("{file}: {} cases, {checked} questions checked", cases.len());
    Ledger::new(test, file).settle(cases.len(), &failures);
}

#[test]
fn every_question_answers_as_the_reference() {
    check("every_question_answers_as_the_reference", FILES[0]);
}

#[test]
fn every_question_of_the_second_file_answers_as_the_reference() {
    check(
        "every_question_of_the_second_file_answers_as_the_reference",
        FILES[1],
    );
}

#[test]
fn every_advanced_entry_answers_as_the_reference() {
    check("every_advanced_entry_answers_as_the_reference", FILES[2]);
}
