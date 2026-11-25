//! Cases a plausible wrong store side of the document frequency memo
//! fails: a memoised answer that is not the uncached one, the transaction
//! flag read from the connection alone, the digest's ingest skipped, the
//! fallback carrying lengths, the body mask taken for body-or-scopes, and
//! the memo's key spelled other than `str(Path)`, the patch's statement
//! reading the oldest hits first under its limit. POSIX only, as the
//! fixture's stores are.

#![cfg(unix)]

mod common;

use std::path::{Path, PathBuf};

use bettermemory_core::store::docfreq::{DocFreqMemo, DocFreqSource};
use bettermemory_core::store::docfreq_store::{self, StoreDocFreq};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use common::docfreq_store::{admit_of, answer, cases, files, lay, lay_step};
use common::opened;
use common::stores::{scratch, Scratch};
use common::txs::Replay;
use serde_json::Value;

fn opened_store(header: &Value) -> (Scratch, PathBuf, WritableStore) {
    let dir = scratch("dfk");
    let (file, keys) = files(&dir.0);
    opened::made("created", file.parent().expect("dir"));
    let store = open_for_writing(&file, &keys, &opened::provider(header)).expect("open");
    (dir, file, store)
}

fn case(name: &str) -> (Value, Value) {
    let (header, all) = cases();
    let case = all
        .into_iter()
        .find(|c| c["case"] == name)
        .expect("the case");
    (header, case)
}

fn terms(t: &[&str]) -> Vec<String> {
    t.iter().map(|s| (*s).to_owned()).collect()
}

/// Lays `name`'s first step and answers `call` natively.
fn first_step(name: &str, call: &str, t: &[&str]) -> (Value, Vec<String>) {
    let (header, case) = case(name);
    let (_dir, file, store) = opened_store(&header);
    lay(&file, &case["steps"][0]["tables"]);
    let src = StoreDocFreq::of_store(&store);
    let memo = DocFreqMemo::new();
    let got = answer(call, &src, &memo, &terms(t), "all");
    let digest_seq = store
        .connection()
        .query_row(
            "SELECT value FROM digest_state WHERE key = 'seq'",
            [],
            |r| r.get::<_, String>(0),
        )
        .unwrap_or_default();
    (got, vec![digest_seq])
}

#[test]
fn the_memoised_answer_is_the_uncached_one_on_every_plain_step() {
    let (header, all) = cases();
    let mut compared = 0;
    for case in &all {
        let (_dir, file, store) = opened_store(&header);
        let memo = DocFreqMemo::new();
        for step in case["steps"].as_array().expect("steps") {
            lay_step(&file, step);
            if step["conn"] != "plain" {
                continue;
            }
            for ask in step["answers"].as_array().expect("answers") {
                let want = &ask[3];
                let t: Vec<String> = ask[1]
                    .as_array()
                    .expect("terms")
                    .iter()
                    .map(|v| v.as_str().expect("t").to_owned())
                    .collect();
                let src = StoreDocFreq::of_store(&store);
                let admit = ask[2].as_str().expect("admit");
                // A question of the digest itself is asked as the reference
                // asked it: a mark moves the digest's state, which no
                // step's tables carry.
                let call = ask[0].as_str().expect("call");
                if !matches!(call, "rank" | "memo" | "plain") {
                    answer(call, &src, &memo, &t, admit);
                    continue;
                }
                let memoised = answer("memo", &src, &memo, &t, admit);
                let plain = answer("plain", &src, &memo, &t, admit);
                if ask[0] == "memo" {
                    assert_eq!(&memoised, want, "{} memo", case["case"]);
                    // The reference's own two answers differ where a planted
                    // row reads in one source and not the other.
                    let reference_plain = step["answers"]
                        .as_array()
                        .expect("answers")
                        .iter()
                        .find(|a| a[0] == "plain" && a[1] == ask[1] && a[2] == ask[2]);
                    if reference_plain.is_some_and(|a| a[3] == *want) {
                        assert_eq!(memoised, plain, "{} memo against plain", case["case"]);
                    }
                    compared += 1;
                }
            }
        }
    }
    assert!(compared > 20, "compared {compared}");
}

#[test]
fn a_rolled_back_transaction_still_reads_uncached() {
    let (header, case) = case("digest_behind");
    let (_dir, file, mut store) = opened_store(&header);
    lay(&file, &case["steps"][0]["tables"]);
    let path = store.path().to_owned();
    let id = store.store_id().to_owned();
    let memo = DocFreqMemo::new();
    let mut clocks = Replay::new(&[], &[]);
    let tx = store.begin(&mut clocks).expect("begin");
    tx.connection().execute_batch("ROLLBACK").expect("rollback");
    assert!(tx.connection().is_autocommit());
    let src = StoreDocFreq::of_transaction(&tx, &path, &id);
    assert!(src.in_transaction());
    let mut admit = admit_of("all");
    let got = docfreq_store::ranking_statistics(&src, &memo, &terms(&["record"]), &mut admit)
        .expect("ranked")
        .expect("found");
    assert_eq!(got.3, None, "inside a transaction there is no average");
    assert!(memo.keys().is_empty(), "the memo is not touched");
    // The connection alone is not in a transaction.
    let over = StoreDocFreq::over(tx.connection(), &path, &id);
    assert!(!over.in_transaction());
}

#[test]
fn a_digest_behind_is_brought_current_first() {
    let (got, seq) = first_step("digest_behind", "rank", &["record"]);
    assert_eq!(got["returned"][3], "4.0", "{got}");
    assert_ne!(seq[0], "", "the digest has a seq");
    let (header, case) = case("digest_behind");
    let (_dir, file, store) = opened_store(&header);
    lay(&file, &case["steps"][0]["tables"]);
    let before: String = store
        .connection()
        .query_row(
            "SELECT value FROM digest_state WHERE key = 'seq'",
            [],
            |r| r.get(0),
        )
        .expect("seq");
    let src = StoreDocFreq::of_store(&store);
    assert!(src.digest_mark().expect("mark").is_some());
    let after: String = store
        .connection()
        .query_row(
            "SELECT value FROM digest_state WHERE key = 'seq'",
            [],
            |r| r.get(0),
        )
        .expect("seq");
    assert_ne!(before, after, "the ingest ran");
    assert!(src.warnings().is_empty());
}

#[test]
fn the_fallback_carries_no_lengths() {
    let (got, _) = first_step("no_digest_state", "rank", &["record"]);
    assert_eq!(got["returned"][0], 3, "{got}");
    assert!(got["returned"][3].is_null(), "{got}");
}

#[test]
fn a_scopes_hit_counts_in_either_and_not_in_body() {
    let (got, _) = first_step("scopes_only", "rank", &["other"]);
    let body = &got["returned"][1];
    let either = &got["returned"][2];
    assert_eq!(body.as_array().expect("body").len(), 0, "{got}");
    assert_eq!(either[0][0], "other", "{got}");
}

#[test]
fn the_key_is_the_path_as_str_spells_it() {
    let (header, _) = case("empty_store");
    let (dir, file, store) = opened_store(&header);
    let root = dir.0.to_str().expect("utf-8").to_owned();
    let src = StoreDocFreq::of_store(&store);
    assert_eq!(src.store_key().0, format!("{root}/c/memory.sqlite"));
    let spelled = |p: &Path| StoreDocFreq::over(store.connection(), p, "x").store_key().0;
    // `..` stays; `.` and a doubled separator go, as PurePath drops them.
    assert_eq!(
        spelled(&dir.0.join("c/../c/memory.sqlite")),
        format!("{root}/c/../c/memory.sqlite")
    );
    assert_eq!(
        spelled(Path::new(&format!("{root}/c/.//memory.sqlite"))),
        format!("{root}/c/memory.sqlite")
    );
    assert_ne!(spelled(&file), spelled(&dir.0.join("c/../c/memory.sqlite")));
}

/// `_patched_masks`' statement reads the highest row numbers first, so a
/// limit that cuts the hits keeps the newest.
#[test]
fn the_patch_keeps_the_newest_hits_under_its_limit() {
    let (header, case) = case("digest_behind");
    let (_dir, file, store) = opened_store(&header);
    lay(&file, &case["steps"][0]["tables"]);
    for rowid in [11, 12, 13, 14] {
        store
            .connection()
            .execute(
                "INSERT INTO memories_fts(rowid, body_fts, scopes_fts) VALUES (?, 'zeta', 'g')",
                [rowid],
            )
            .expect("fts row");
    }
    let all: Vec<i64> = store
        .connection()
        .prepare("SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'zeta' ORDER BY rowid")
        .expect("prepare")
        .query_map([], |r| r.get(0))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows");
    assert!(all.len() > 2, "hits {all:?}");
    let src = StoreDocFreq::of_store(&store);
    let got = src.fts_rowids("zeta", 0, 2).expect("fts");
    let newest: Vec<i64> = all.iter().rev().take(2).copied().collect();
    assert_eq!(got, newest);
}
