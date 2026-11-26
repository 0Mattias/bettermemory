//! Kills for the search pool's route through the document frequency memo
//! (`store::pool` over `store::docfreq` and `docfreq_store::admitted`):
//! each test is an input on which a plausible wrong pool answers
//! otherwise. Inputs kept for another store or another memory head, a memo
//! keyed by the private copy's path, statistics from the memo inside a
//! transaction, unknown rows read one at a time, inputs read for no row,
//! the share test overflowing, an empty query that is not paged, a narrow
//! request that is not ranked whole, and a mask position off by one past
//! a 64-row word. Each answer is compared with the rows a plain
//! connection reads and filters itself. Where the rows are the same by
//! design, the texts a pool sends (counted by the statement cache) tell
//! the reads apart: the batch of 500, the share test at equality, the
//! first read's 800 rows and a pool it fills exactly, the best rows
//! read first inside a transaction, and no advance over a digest behind
//! the log. Then the rows the inputs lack decided from their own columns
//! (scopes that are no list, the writer's client and model), the empty
//! query's pages, its refusal of a scope that is no string,
//! `ranked_admitted_rows` called directly, and `PoolReader::new` through
//! the process's memo.

mod common;
mod pool_memo;

use std::path::Path;

use bettermemory_core::pyvalue::PyError;
use bettermemory_core::store::docfreq::{self, DocFreqMemo};
use bettermemory_core::store::docfreq_store::{self, StoreDocFreq};
use bettermemory_core::store::pool::{Filters, PoolError, PoolReader, FIRST_READ, POOL_SIZE};
use bettermemory_core::store::record_reads::{get_many, get_many_python_ids, ReadError};
use bettermemory_core::store::versions::path_text;
use bettermemory_core::store::{digest, StoreError, StoreFile};
use bettermemory_core::{lexicon, unicode};
use pool_memo::{expected, store, Digest};
use rusqlite::Connection;

/// `scope` for the copies `hit` names, `other` for the rest.
fn split(hit: impl Fn(i64) -> bool + 'static) -> impl Fn(i64) -> Vec<String> {
    move |k| vec![if hit(k) { "hit" } else { "other" }.to_owned()]
}

/// A pool drawn on a fresh open of `file` through `memo`, for `query` and
/// the scope `scope` (all rows for None), `size` rows.
fn draw(
    file: &Path,
    memo: &DocFreqMemo,
    query: &str,
    scope: Option<&str>,
    size: usize,
) -> Vec<i64> {
    let store = StoreFile::open(file).expect("open");
    let reader = PoolReader::with_memo(&store, memo);
    let scopes = scope.map(|s| vec![s.to_owned()]);
    let filters = Filters {
        scopes: scopes.as_deref(),
        ..Filters::default()
    };
    reader
        .resolve_search_pool(query, &filters, size, Some(digest::fingerprint()))
        .expect("a pool")
        .rowids
}

fn first(rows: Vec<i64>, size: usize) -> Vec<i64> {
    rows.into_iter().take(size).collect()
}

#[test]
fn inputs_kept_for_one_store_are_not_another_s() {
    // Two copies of one store (one store id) at two paths, their rows'
    // scopes the other way round.
    let a = store(100, 1, &split(|k| k % 2 == 0), Digest::Gone);
    let b = store(100, 1, &split(|k| k % 2 == 1), Digest::Gone);
    let memo = DocFreqMemo::new();
    for file in [&a.file, &b.file, &a.file] {
        let want = first(expected(file, Some("postgres"), "hit"), POOL_SIZE);
        assert_eq!(draw(file, &memo, "postgres", Some("hit"), POOL_SIZE), want);
        let want = first(expected(file, None, "hit"), POOL_SIZE);
        assert_eq!(draw(file, &memo, "", Some("hit"), POOL_SIZE), want);
    }
    assert_eq!(memo.keys().len(), 2);
}

/// A record mutation by a plain connection: `rid`'s scopes set to `scope`
/// and a `memory_put` row naming it, which moves the memory head.
fn rewrite(file: &Path, rid: i64, scope: &str) {
    let conn = Connection::open(file).expect("open");
    let json = format!("[\"{scope}\"]");
    conn.execute(
        "UPDATE memories SET scopes_json = ?, scopes_text = ?, scopes_fts = ? WHERE rowid = ?",
        rusqlite::params![json, format!(" {scope} "), scope, rid],
    )
    .expect("update");
    let payload = serde_json::json!({"memory": {"id": pool_memo::id(rid)}}).to_string();
    conn.execute(
        "INSERT INTO log (seq, ts, session, kind, payload, prev_mac, mac) \
         SELECT MAX(seq) + 1, '2026-06-01T12:00:00Z', NULL, 'memory_put', ?1, '', 'w' || ?2 \
         FROM log",
        rusqlite::params![payload, rid],
    )
    .expect("log row");
}

#[test]
fn inputs_kept_at_one_head_are_not_another_s() {
    let s = store(100, 1, &split(|k| k % 2 == 0), Digest::Gone);
    let memo = DocFreqMemo::new();
    let before = draw(&s.file, &memo, "postgres", Some("hit"), POOL_SIZE);
    assert_eq!(before, expected(&s.file, Some("postgres"), "hit"));
    assert!(!before.contains(&1));
    // Row 1 moves into the request, row 2 out of it.
    rewrite(&s.file, 1, "hit");
    rewrite(&s.file, 2, "other");
    let after = draw(&s.file, &memo, "postgres", Some("hit"), POOL_SIZE);
    assert_eq!(after, expected(&s.file, Some("postgres"), "hit"));
    assert!(after.contains(&1) && !after.contains(&2));
    assert_eq!(
        draw(&s.file, &memo, "", Some("hit"), POOL_SIZE),
        expected(&s.file, None, "hit")
    );
}

#[test]
fn the_memo_is_keyed_by_the_path_the_store_was_opened_by() {
    let s = store(10, 1, &split(|_| true), Digest::Base);
    let memo = DocFreqMemo::new();
    let opened = StoreFile::open(&s.file).expect("open");
    // The base is in WAL mode with no live `-wal`: the open reads a
    // private copy, which the key must not name.
    assert!(opened.reads_a_copy());
    let reader = PoolReader::with_memo(&opened, &memo);
    reader
        .resolve_search_pool(
            "postgres",
            &Filters::default(),
            5,
            Some(digest::fingerprint()),
        )
        .expect("a pool");
    let path = unicode::to_string(&path_text(&s.file)).expect("a path");
    assert_eq!(
        memo.keys(),
        vec![(path.clone(), common::stores::store_id())]
    );
    // A second open finds the entry: the memo still holds one store.
    draw(&s.file, &memo, "postgres", None, 5);
    assert_eq!(memo.keys().len(), 1);
    // Under rules this build does not compute, inputs are kept apart.
    let reader = PoolReader::with_memo(&opened, &memo);
    reader
        .resolve_search_pool("postgres", &Filters::default(), 5, Some("other rules"))
        .expect("a pool");
    reader
        .resolve_search_pool("postgres", &Filters::default(), 5, None)
        .expect("a pool");
    assert_eq!(memo.keys().len(), 3);
    assert!(memo.keys().iter().all(|(p, _)| *p == path));
}

#[test]
fn statistics_inside_a_transaction_are_read_not_remembered() {
    // The base's two records, in the digest with their lengths.
    let s = store(0, 1, &split(|_| true), Digest::Base);
    let conn_file = s.file.clone();
    let memo = DocFreqMemo::new();
    let opened = StoreFile::open(&conn_file).expect("open");
    let reader = PoolReader::with_memo(&opened, &memo);
    let terms = ["postgres".to_owned()];
    let fp = Some(digest::fingerprint());
    let outside = reader
        .resolve_search_pool("postgres", &Filters::default(), 2, fp)
        .expect("a pool")
        .statistics(&terms)
        .expect("statistics")
        .expect("some");
    assert!(outside.avgdl.is_some(), "the digest's lengths");
    assert_eq!(memo.keys().len(), 1);
    // Inside a transaction, on a memo of its own: nothing is kept and no
    // average is given.
    let fresh = DocFreqMemo::new();
    let reader = PoolReader::with_memo(&opened, &fresh);
    opened
        .read_transaction(|_| {
            let pool = reader
                .resolve_search_pool("postgres", &Filters::default(), 2, fp)
                .expect("a pool");
            let inside = pool.statistics(&terms).expect("statistics").expect("some");
            assert_eq!(inside.avgdl, None);
            assert_eq!(
                (inside.size, &inside.body_df, &inside.scope_df),
                (outside.size, &outside.body_df, &outside.scope_df)
            );
        })
        .expect("a transaction");
    assert!(fresh.keys().is_empty());
}

/// What a measured run does on a fresh open of the store and a reader
/// through the memo given.
type Run<'a> = &'a dyn Fn(&StoreFile, &PoolReader<'_>);

/// Whether a full-record lookup warmed before `run` (on one connection,
/// through `memo`) was evicted from a statement cache of `capacity` (the
/// run sent `capacity` or more distinct texts) or kept.
fn evicted_by(file: &Path, memo: &DocFreqMemo, capacity: usize, run: Run<'_>) -> bool {
    let opened = StoreFile::open(file).expect("open");
    let conn = opened.connection();
    conn.set_prepared_statement_cache_capacity(capacity);
    let reader = PoolReader::with_memo(&opened, memo);
    assert!(get_many(conn, &["absent".to_owned()])
        .expect("lookup")
        .is_empty());
    run(&opened, &reader);
    conn.prepare("SELECT missing FROM statement_cache_missing_table")
        .expect_err("no such table");
    !matches!(
        get_many_python_ids(conn, &[vec![0xd800]]).expect_err("a lone surrogate"),
        ReadError::Bind { .. }
    )
}

/// The distinct texts a run sends, `trial(capacity)` answering whether
/// the lookup was evicted: the least capacity that keeps the lookup, less
/// the lookup's own place.
fn least(trial: impl Fn(usize) -> bool) -> usize {
    (1..64)
        .find(|&capacity| !trial(capacity))
        .expect("a capacity that keeps the lookup")
        - 1
}

/// A pool for `query` and the scope `scope` (all rows for None), `size`
/// rows, through `reader`.
fn pool_rows(reader: &PoolReader<'_>, query: &str, scope: Option<&str>, size: usize) -> Vec<i64> {
    let scopes = scope.map(|s| vec![s.to_owned()]);
    let filters = Filters {
        scopes: scopes.as_deref(),
        ..Filters::default()
    };
    reader
        .resolve_search_pool(query, &filters, size, Some(digest::fingerprint()))
        .expect("a pool")
        .rowids
}

/// The distinct texts `pools` (each a query and a size, drawn in turn on
/// one connection and a memo of their own) send, for the scope `scope`.
fn texts_in(file: &Path, scope: Option<&str>, pools: &[(&str, usize)]) -> usize {
    least(|capacity| {
        evicted_by(file, &DocFreqMemo::new(), capacity, &|_, reader| {
            for (query, size) in pools {
                pool_rows(reader, query, scope, *size);
            }
        })
    })
}

/// [`texts_in`] for every row.
fn texts(file: &Path, pools: &[(&str, usize)]) -> usize {
    texts_in(file, None, pools)
}

/// The distinct texts the newest pool of 600 rows the inputs do not hold
/// sends: the memory head; the digest's state, the log row at its mark,
/// its head and its rows; the newest rows; the rows' own inputs in two
/// batches (500 and 100); the stored tokenizer; and the records in two
/// batches.
const NEWEST_600_UNKNOWN: usize = 11;

/// The same for 500 rows: one batch of each.
const NEWEST_500_UNKNOWN: usize = NEWEST_600_UNKNOWN - 2;

#[test]
fn unknown_rows_are_read_in_batches_of_500() {
    let s = store(600, 1000, &split(|_| true), Digest::Base);
    let memo = DocFreqMemo::new();
    // The base's two records are the inputs; the 600 copies are not.
    assert_eq!(
        draw(&s.file, &memo, "", None, 600),
        first(expected(&s.file, None, "hit"), 600)
    );
    // One statement a batch: one text per batch size, so a row read by a
    // statement of its own, or every row by one statement, sends one text
    // fewer.
    assert_eq!(texts(&s.file, &[("", 600)]), NEWEST_600_UNKNOWN);
    // A store of 500 such rows is read in one batch and one of 501 in
    // two: the batch is 500, neither less nor more.
    for (rows, want) in [(500, NEWEST_500_UNKNOWN), (501, NEWEST_600_UNKNOWN)] {
        let s = store(rows, 1000, &split(|_| true), Digest::Base);
        assert_eq!(texts(&s.file, &[("", FIRST_READ)]), want, "{rows} rows");
    }
}

#[test]
fn admitted_rows_of_no_row_read_no_input() {
    let s = store(0, 1, &split(|_| true), Digest::Gone);
    let conn = Connection::open(&s.file).expect("open");
    conn.execute_batch("DELETE FROM memories").expect("delete");
    drop(conn);
    let memo = DocFreqMemo::new();
    assert!(draw(&s.file, &memo, "", None, POOL_SIZE).is_empty());
    assert!(memo.keys().is_empty(), "the newest rows were none");
    // A query asks the share first, which reads the inputs.
    assert!(draw(&s.file, &memo, "postgres", None, POOL_SIZE).is_empty());
    assert_eq!(memo.keys().len(), 1);
}

/// The texts of a wide pool (`... LIMIT ?`) on a store whose every row is
/// admitted and whose digest is gone: the memory head, the digest's state
/// and the memory rows (the inputs), the index's best rows, the stored
/// tokenizer and the records. A second wide pool of two sends no other; a
/// second pool of any size past 800 sends the records' text for all 50
/// rows and, being narrow, the whole ranking (a wide one would not).
const WIDE: usize = 6;
const WIDE_THEN_NARROW: usize = 8;

#[test]
fn the_share_test_does_not_overflow() {
    let s = store(50, 1, &split(|k| k % 2 == 0), Digest::Gone);
    let memo = DocFreqMemo::new();
    for size in [usize::MAX, usize::MAX / 800 + 1, 1 << 62] {
        assert_eq!(
            draw(&s.file, &memo, "postgres", Some("hit"), size),
            expected(&s.file, Some("postgres"), "hit")
        );
        assert_eq!(
            draw(&s.file, &memo, "", Some("hit"), size),
            expected(&s.file, None, "hit")
        );
    }
    // The rows are the same either way; the share decides the texts. 50
    // of 50 admitted: wide for a pool of 2 (40,000 >= 100), narrow for any
    // size past 800, where a product that wrapped would read wide.
    assert_eq!(texts(&s.file, &[("postgres", 2), ("postgres", 2)]), WIDE);
    for size in [801, usize::MAX, usize::MAX / 50 + 1, 1 << 62] {
        assert_eq!(
            texts(&s.file, &[("postgres", 2), ("postgres", size)]),
            WIDE_THEN_NARROW,
            "size {size}"
        );
    }
}

#[test]
fn the_empty_query_pages_past_the_first_read() {
    // The newest are the highest copies: the request admits only the
    // oldest hundred, past two reads of 800.
    let s = store(1800, 1, &split(|k| k <= 100), Digest::Gone);
    let memo = DocFreqMemo::new();
    let want = expected(&s.file, None, "hit");
    assert_eq!(want.len(), 100);
    assert_eq!(draw(&s.file, &memo, "", Some("hit"), POOL_SIZE), want);
    assert_eq!(draw(&s.file, &memo, "", Some("hit"), 40), first(want, 40));
}

#[test]
fn a_narrow_request_reads_the_whole_ranking() {
    // Every copy matches with the same score: the ranking is by row
    // number, so the admitted rows past the first 800 are found only by
    // the batched read of the whole ranking.
    let s = store(2000, 1, &split(|k| k == 1500 || k == 1999), Digest::Gone);
    let memo = DocFreqMemo::new();
    let want = expected(&s.file, Some("postgres"), "hit");
    assert_eq!(want, vec![1500, 1999]);
    assert_eq!(
        draw(&s.file, &memo, "postgres", Some("hit"), POOL_SIZE),
        want
    );
    assert_eq!(draw(&s.file, &memo, "postgres", Some("hit"), 1), vec![1500]);
}

#[test]
fn a_wide_request_short_of_its_pool_reads_the_ranking_again() {
    // 600 of 2,000 admitted: wide by the share (600 * 800 >= 200 * 2,000),
    // but none of them in the index's first 800, so the first read falls
    // short and the ranking is read again from the start.
    let s = store(2000, 1, &split(|k| k > 1400), Digest::Gone);
    let memo = DocFreqMemo::new();
    let want = first(expected(&s.file, Some("postgres"), "hit"), POOL_SIZE);
    assert_eq!(want[0], 1401);
    assert_eq!(
        draw(&s.file, &memo, "postgres", Some("hit"), POOL_SIZE),
        want
    );
    const { assert!(FIRST_READ < 1400) };
}

#[test]
fn a_mask_position_past_64_rows_is_its_own() {
    // The memory rows in scan order: copy `k` at position `k - 1`, so the
    // admitted positions are each side of the first two word boundaries.
    let hits = [63, 64, 65, 66, 128, 129, 130];
    let s = store(130, 1, &split(move |k| hits.contains(&k)), Digest::Gone);
    let memo = DocFreqMemo::new();
    let want = expected(&s.file, Some("postgres"), "hit");
    assert_eq!(want, hits.to_vec());
    assert_eq!(
        draw(&s.file, &memo, "postgres", Some("hit"), POOL_SIZE),
        want
    );
    assert_eq!(
        draw(&s.file, &memo, "", Some("hit"), POOL_SIZE),
        expected(&s.file, None, "hit")
    );
    let opened = StoreFile::open(&s.file).expect("open");
    let reader = PoolReader::with_memo(&opened, &memo);
    let scopes = vec!["hit".to_owned()];
    let filters = Filters {
        scopes: Some(&scopes),
        ..Filters::default()
    };
    let pool = reader
        .resolve_search_pool("postgres", &filters, POOL_SIZE, Some(digest::fingerprint()))
        .expect("a pool");
    // The index holds the stemmed word.
    let stem = lexicon::fts_index_text(&unicode::cps("postgres"));
    let stats = pool
        .statistics(&[stem.clone(), "w64".to_owned(), "w67".to_owned()])
        .expect("statistics")
        .expect("some");
    assert_eq!(stats.size, 7);
    assert_eq!(stats.body_df.get("w64"), Some(&1));
    assert_eq!(stats.body_df.get("w67"), None);
    assert_eq!(stats.body_df.get(&stem), Some(&7));
}

#[test]
fn the_share_test_holds_at_equality() {
    // 50 of 50 admitted with a pool of 800: 50 * 800 >= 800 * 50, wide.
    // After a wide pool of two, a second wide pool sends only the records'
    // text for its 50 rows; a narrow one would send the whole ranking too.
    let s = store(50, 1, &split(|k| k % 2 == 0), Digest::Gone);
    assert_eq!(
        texts(&s.file, &[("postgres", 2), ("postgres", FIRST_READ)]),
        WIDE + 1
    );
    assert_eq!(
        texts(&s.file, &[("postgres", 2), ("postgres", FIRST_READ + 1)]),
        WIDE_THEN_NARROW
    );
}

#[test]
fn a_first_read_that_fills_the_pool_exactly_is_enough() {
    // 400 of 1,000 admitted (wide by the share), exactly 200 of them in
    // the index's first 800: the pool is full, so the ranking is not read
    // again.
    let s = store(1000, 1, &split(|k| k % 4 == 0 || k > 800), Digest::Gone);
    let want = first(expected(&s.file, Some("postgres"), "hit"), POOL_SIZE);
    assert_eq!(want.last(), Some(&800));
    let memo = DocFreqMemo::new();
    assert_eq!(
        draw(&s.file, &memo, "postgres", Some("hit"), POOL_SIZE),
        want
    );
    assert_eq!(
        texts_in(&s.file, Some("hit"), &[("postgres", POOL_SIZE)]),
        WIDE
    );
}

#[test]
fn the_first_read_is_800_rows() {
    // 600 of 2,000 admitted, none in the index's first 800 and 200 in the
    // next: the first read falls short and the whole ranking is read, one
    // text more than a wide pool. A first read of 1,600 rows would fill
    // the pool.
    let s = store(2000, 1, &split(|k| k > 1400), Digest::Gone);
    assert_eq!(
        texts_in(&s.file, Some("hit"), &[("postgres", POOL_SIZE)]),
        WIDE + 1
    );
}

#[test]
fn a_pool_inside_a_transaction_reads_the_best_rows_first() {
    // No share inside a transaction: the pool reads the index's best rows
    // (`... LIMIT ?`), the text a wide pool before it sent, and decides
    // the 50 rows from their own columns (one text). A narrow read would
    // send the whole ranking too.
    let s = store(50, 1, &split(|k| k % 2 == 0), Digest::Gone);
    let outside_then_inside = least(|capacity| {
        evicted_by(&s.file, &DocFreqMemo::new(), capacity, &|opened, reader| {
            assert_eq!(pool_rows(reader, "postgres", None, 2), vec![1, 2]);
            let inside = opened
                .read_transaction(|_| pool_rows(reader, "postgres", None, 2))
                .expect("a transaction");
            assert_eq!(inside, vec![1, 2]);
        })
    });
    assert_eq!(outside_then_inside, WIDE + 1);
}

#[test]
fn a_kept_entry_is_not_advanced_over_a_digest_behind_the_log() {
    // Inputs kept with the digest's lengths, then a record mutation by a
    // plain connection: the digest is behind, so the entry is read again
    // from the digest brought forward in memory, never advanced from the
    // digest's rows (which still hold the old scopes).
    let trial = |capacity: usize| -> bool {
        let s = store(100, 1, &split(|k| k % 2 == 0), Digest::Copies);
        let memo = DocFreqMemo::new();
        assert!(!draw(&s.file, &memo, "postgres", Some("hit"), POOL_SIZE).contains(&1));
        rewrite(&s.file, 1, "hit");
        let found = std::cell::RefCell::new(Vec::new());
        let gone = evicted_by(&s.file, &memo, capacity, &|_, reader| {
            *found.borrow_mut() = pool_rows(reader, "postgres", Some("hit"), POOL_SIZE);
        });
        assert_eq!(
            found.into_inner(),
            expected(&s.file, Some("postgres"), "hit"),
            "capacity {capacity}"
        );
        gone
    };
    assert_eq!(least(trial), BEHIND_KEPT);
}

/// The texts of a pool whose kept entry is behind a digest that is behind
/// the log: the memory head; the log row at the entry's head and the log
/// rows since, read before the digest is asked, as the reference reads
/// them before `digest_mark(build=False)`; the digest's state, the log
/// row at its mark (the text the entry's head was read by) and its head
/// (asked whether to advance, then again for its rows); its rows and the
/// mutated record's home; the ranking, the stored tokenizer and the
/// records.
const BEHIND_KEPT: usize = 10;

/// A store of ten copies past the base's records (rows the memo's inputs
/// do not hold), with `edit` applied by a plain connection.
fn unknown_rows(edit: &str) -> pool_memo::PoolStore {
    let s = store(10, 1000, &split(|_| true), Digest::Base);
    let conn = Connection::open(&s.file).expect("open");
    conn.execute_batch(edit).expect("the edit");
    s
}

/// A pool of every row the filters admit, newest first or ranked.
fn pool_with(file: &Path, query: &str, filters: &Filters<'_>) -> Result<Vec<i64>, PoolError> {
    let opened = StoreFile::open(file).expect("open");
    let memo = DocFreqMemo::new();
    let reader = PoolReader::with_memo(&opened, &memo);
    reader
        .resolve_search_pool(query, filters, POOL_SIZE, Some(digest::fingerprint()))
        .map(|pool| pool.rowids)
}

/// The copies' row numbers but `left_out`, in the index's order for
/// `postgres` (the row number: every copy scores the same) and newest
/// first.
fn copies_but(left_out: &[i64]) -> (Vec<i64>, Vec<i64>) {
    let ranked: Vec<i64> = (1000..1010).filter(|r| !left_out.contains(r)).collect();
    let newest = ranked.iter().rev().copied().collect();
    (ranked, newest)
}

#[test]
fn a_row_the_inputs_lack_whose_scopes_are_no_list_is_admitted_nowhere() {
    let s = unknown_rows(
        "UPDATE memories SET scopes_json = 'null' WHERE rowid = 1003;
         UPDATE memories SET scopes_json = '{}' WHERE rowid = 1006;",
    );
    let (ranked, newest) = copies_but(&[1003, 1006]);
    let all = Filters::default();
    assert_eq!(
        pool_with(&s.file, "postgres", &all).expect("a pool"),
        ranked
    );
    assert_eq!(pool_with(&s.file, "", &all).expect("a pool"), newest);
}

#[test]
fn a_row_the_inputs_lack_is_decided_by_its_own_writer() {
    // Copy 2 (row 1001) has a model and no client; copy 3 a client and a
    // model; copy 4 the same two the other way round; the rest no writer.
    let s = unknown_rows(
        "UPDATE memories SET actor_client = NULL, actor_model = NULL;
         UPDATE memories SET actor_model = 'm1' WHERE rowid = 1001;
         UPDATE memories SET actor_client = 'c1', actor_model = 'm2' WHERE rowid = 1002;
         UPDATE memories SET actor_client = 'm2', actor_model = 'c1' WHERE rowid = 1003;",
    );
    for (client, model, want) in [
        (Some("c1"), None, vec![1002]),
        (None, Some("m1"), vec![1001]),
        (None, Some("m2"), vec![1002]),
        (Some("m2"), None, vec![1003]),
        (Some("c1"), Some("m2"), vec![1002]),
    ] {
        let filters = Filters {
            client,
            model,
            ..Filters::default()
        };
        let newest: Vec<i64> = want.iter().rev().copied().collect();
        assert_eq!(
            pool_with(&s.file, "postgres", &filters).expect("a pool"),
            want,
            "{client:?} {model:?}"
        );
        assert_eq!(
            pool_with(&s.file, "", &filters).expect("a pool"),
            newest,
            "{client:?} {model:?}"
        );
    }
}

#[test]
fn the_empty_query_reads_each_row_once_across_pages() {
    // The 800th newest row is the last of the first page; the rest of the
    // request's rows are on the third.
    let s = store(1700, 1, &split(|k| k == 901 || k <= 5), Digest::Gone);
    let want = expected(&s.file, None, "hit");
    assert_eq!(want, vec![901, 5, 4, 3, 2, 1]);
    let memo = DocFreqMemo::new();
    assert_eq!(draw(&s.file, &memo, "", Some("hit"), POOL_SIZE), want);
}

#[test]
fn a_scope_that_is_no_string_is_refused_on_the_empty_query_s_pages() {
    // Read through the inputs (the digest gone) and from the row's own
    // columns (a row the inputs lack).
    for held in [Digest::Gone, Digest::Base] {
        let s = store(10, 1000, &split(|_| true), held);
        let conn = Connection::open(&s.file).expect("open");
        conn.execute_batch("UPDATE memories SET scopes_json = '[1]' WHERE rowid = 1004")
            .expect("the edit");
        drop(conn);
        for query in ["", "postgres"] {
            let refused = pool_with(&s.file, query, &Filters::default());
            assert!(
                matches!(
                    refused,
                    Err(PoolError::Store(StoreError::Py(PyError::TypeError)))
                ),
                "{held:?} {query:?}: {:?}",
                refused.map_err(|e| format!("{e:?}"))
            );
        }
    }
}

#[test]
fn ranked_admitted_rows_stops_at_its_size_and_cuts_to_it() {
    let s = unknown_rows("UPDATE memories SET scopes_json = 'not json' WHERE rowid = 1005");
    let conn = Connection::open(&s.file).expect("open");
    let src = StoreDocFreq::over(&conn, &s.file, &common::stores::store_id());
    let memo = DocFreqMemo::new();
    let ranked = |size: usize, batch: i64| {
        docfreq_store::ranked_admitted_rows(
            &src,
            &conn,
            &memo,
            "postgres",
            &mut |_, _, _| true,
            size,
            batch,
        )
        .map_err(|e| format!("{e:?}"))
    };
    // Five found at the end of the first batch of five: the second, whose
    // first row does not load, is never read.
    assert_eq!(ranked(5, 5), Ok((1000..1005).collect()));
    assert!(ranked(6, 5)
        .expect_err("the sixth row")
        .contains("JSONDecodeError"));
    // A batch of five past a size of one: the first row only.
    assert_eq!(ranked(1, 5), Ok(vec![1000]));
}

#[test]
fn a_reader_of_its_own_reads_through_the_process_memo() {
    let s = store(10, 1, &split(|_| true), Digest::Copies);
    let path = unicode::to_string(&path_text(&s.file)).expect("a path");
    let key = (path, common::stores::store_id());
    assert!(!docfreq::memo().keys().contains(&key));
    let want = first(expected(&s.file, Some("postgres"), "hit"), 5);
    for _ in 0..2 {
        let opened = StoreFile::open(&s.file).expect("open");
        let reader = PoolReader::new(&opened);
        let pool = reader
            .resolve_search_pool(
                "postgres",
                &Filters::default(),
                5,
                Some(digest::fingerprint()),
            )
            .expect("a pool");
        assert_eq!(pool.rowids, want);
        assert!(docfreq::memo().keys().contains(&key));
    }
}
