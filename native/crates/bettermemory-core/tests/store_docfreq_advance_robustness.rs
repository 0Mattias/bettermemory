//! Hostile input for the memo entry's advance (`_advanced_inputs`) over a
//! store made by hand (`common::docfreq_store::HandStore`): 256 changed
//! rows on an entry of 200,000 with no copy of the entry per row, a
//! payload nested 100,000 deep, 500 and 501 ids in one span, row numbers
//! at the ends of `i64` with lengths whose sum passes it, numbers past 64
//! bits (a refused actor on the same row raised first), an entry at the
//! last version, entries whose arrays disagree, and
//! eight threads advancing one store's entry through one memo. Runs
//! everywhere.

mod common;
mod timing;

use std::path::Path;
use std::sync::{Condvar, Mutex};
use std::time::Duration;

use bettermemory_core::json::MAX_DEPTH;
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::store::docfreq::{
    AdmissionRow, DocFreqError, DocFreqInputs, DocFreqMemo, DocFreqSource, Head, Mask, PyInt,
    Ranked,
};
use bettermemory_core::store::docfreq_store::{self, StoreDocFreq};
use common::docfreq_store::HandStore;
use rusqlite::Connection;

const PATH: &str = "/nowhere/memory.sqlite";

fn hand() -> HandStore {
    HandStore::new(Connection::open_in_memory().expect("memory"))
}

/// An entry of `rows` rows in eight groups at `head`, as the grouping of
/// a full read makes it.
fn entry_of(rows: i64, head: (i64, String)) -> DocFreqInputs {
    let read: Vec<AdmissionRow> = (1..=rows)
        .map(|rid| AdmissionRow {
            rid: rid.into(),
            scopes_text: format!("[\"s{}\"]", rid % 8),
            home_project: None,
            client: None,
            model: None,
            length: rid % 13,
        })
        .collect();
    DocFreqInputs::from_rows(head, &read, true).expect("grouped")
}

#[test]
fn an_advance_of_256_rows_copies_the_entry_once() {
    let mut s = hand();
    let head0 = s.log_row("home_put", "{\"home\": {\"record_id\": \"zz\"}}", false);
    let entry = entry_of(200_000, head0);
    s.conn.execute_batch("BEGIN").expect("begin");
    for k in 0..256 {
        // Half the rows held, moved to another group; half new.
        let rid = if k % 2 == 0 { 1 + k * 700 } else { 300_000 + k };
        s.put(&format!("r{k}"), rid, "[\"moved\"]", None, 3);
    }
    s.conn.execute_batch("COMMIT").expect("commit");
    let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
    let head = src.memory_head().expect("head");
    let got = src
        .advanced_inputs(&entry, &head)
        .expect("advance")
        .expect("advanced");
    assert_eq!(got.changed.len(), 256);
    assert_eq!(got.index.len(), 200_128);
    assert_eq!(got.groups.len(), 9);
    timing::grows_within(
        "an advance of 256 rows against a copy of a 200,000-row entry",
        4,
        &mut || timing::once(|| entry.clone()),
        &mut || timing::once(|| src.advanced_inputs(&entry, &head).expect("advance")),
    );
}

fn nested(lists: usize) -> String {
    format!(
        "{{\"record_id\": {}{}}}",
        "[".repeat(lists),
        "]".repeat(lists)
    )
}

#[test]
fn a_payload_nested_100000_deep_raises_recursion_error() {
    // The port's limit, `json::MAX_DEPTH`: the 9,999th nested container
    // raises, the payload's object among them. That is CPython 3.13.13's
    // at a script's top level; inside the advance the reference raises
    // some twenty containers sooner (its callers have used part of the C
    // stack), and 3.14 on Linux reads deeper. RecursionError is not
    // caught by the advance, so the memo's call raises it.
    for (lists, raises) in [(MAX_DEPTH - 1, false), (MAX_DEPTH, true), (100_000, true)] {
        let mut s = hand();
        s.put("a", 1, "[\"g\"]", None, 1);
        let memo = DocFreqMemo::new();
        let t = vec!["x".to_owned()];
        let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
        let entry = src
            .read_inputs(&src.memory_head().expect("head"))
            .expect("read");
        docfreq_store::ranking_statistics(&src, &memo, &t, &mut |_, _, _| true).expect("kept");
        drop(src);
        s.log_row("home_delete", &nested(lists), false);
        let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
        let head = src.memory_head().expect("head");
        let advanced = src.advanced_inputs(&entry, &head);
        let memoised = docfreq_store::ranking_statistics(&src, &memo, &t, &mut |_, _, _| true);
        let recursion = DocFreqError::Py(PyError::RecursionError);
        if raises {
            assert_eq!(advanced, Err(recursion.clone()), "{lists}");
            assert_eq!(memoised, Err(recursion), "{lists}");
        } else {
            assert!(advanced.expect("read").is_some(), "{lists}");
            assert!(memoised.is_ok(), "{lists}");
        }
    }
}

#[test]
fn five_hundred_and_501_ids_pass_the_bound_in_one_span() {
    for count in [500, 501] {
        let mut s = hand();
        s.put("a", 1, "[\"g\"]", None, 1);
        let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
        let entry = src
            .read_inputs(&src.memory_head().expect("head"))
            .expect("read");
        drop(src);
        s.conn.execute_batch("BEGIN").expect("begin");
        for k in 0..count {
            s.put(&format!("r{k}"), 10 + k, "[\"g\"]", None, 1);
        }
        s.conn.execute_batch("COMMIT").expect("commit");
        let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
        let head = src.memory_head().expect("head");
        assert_eq!(src.advanced_inputs(&entry, &head), Ok(None), "{count}");
    }
}

#[test]
fn row_numbers_at_the_ends_of_i64_and_lengths_past_it() {
    let mut s = hand();
    s.put("a", 1, "[\"g\"]", None, 1);
    let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
    let entry = src
        .read_inputs(&src.memory_head().expect("head"))
        .expect("read");
    drop(src);
    s.put("low", i64::MIN, "[\"g\"]", None, i64::MAX);
    s.put("high", i64::MAX, "[\"g\"]", None, i64::MAX);
    let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
    let head = src.memory_head().expect("head");
    let got = src
        .advanced_inputs(&entry, &head)
        .expect("advance")
        .expect("advanced");
    // The rows come in the digest's id index order: "high" first.
    assert_eq!(got.index.get(&i64::MAX.into()), Some(&1));
    assert_eq!(got.index.get(&i64::MIN.into()), Some(&2));
    assert_eq!(got.lengths, Some(vec![1 + 2 * i128::from(i64::MAX)]));
    assert_eq!(
        got.changed,
        vec![(1, i64::MAX.into(), 1), (1, i64::MIN.into(), 2)]
    );
}

/// A store connection as the memo's source that counts the advances that
/// answered, and holds each one until every thread has advanced (or ten
/// seconds passed), so no thread keeps its entry before the others ask.
struct Counted<'c> {
    inner: StoreDocFreq<'c>,
    advanced: &'c (Mutex<usize>, Condvar),
    threads: usize,
}

impl DocFreqSource for Counted<'_> {
    fn memory_head(&self) -> Result<Head, DocFreqError> {
        self.inner.memory_head()
    }
    fn store_key(&self) -> (String, String) {
        self.inner.store_key()
    }
    fn in_transaction(&self) -> bool {
        self.inner.in_transaction()
    }
    fn admission_rows(&self) -> Result<(Vec<AdmissionRow>, bool), DocFreqError> {
        self.inner.admission_rows()
    }
    fn read_inputs(&self, head: &Head) -> Result<DocFreqInputs, DocFreqError> {
        self.inner.read_inputs(head)
    }
    fn advanced_inputs(
        &self,
        entry: &DocFreqInputs,
        head: &Head,
    ) -> Result<Option<DocFreqInputs>, DocFreqError> {
        let got = self.inner.advanced_inputs(entry, head)?;
        if got.is_some() {
            let (count, all) = self.advanced;
            let mut n = count.lock().expect("count");
            *n += 1;
            all.notify_all();
            let wait = Duration::from_secs(10);
            drop(
                all.wait_timeout_while(n, wait, |n| *n < self.threads)
                    .expect("wait"),
            );
        }
        Ok(got)
    }
    fn term_masks(&self, term: &str, inputs: &DocFreqInputs) -> Result<(Mask, Mask), DocFreqError> {
        self.inner.term_masks(term, inputs)
    }
    fn fts_rowids(&self, expr: &str, low: i64, limit: usize) -> Result<Vec<i64>, DocFreqError> {
        self.inner.fts_rowids(expr, low, limit)
    }
}

#[test]
fn eight_threads_advance_one_stores_entry() {
    let dir = common::stores::scratch("dfr");
    let file = dir.0.join("memory.sqlite");
    let mut s = HandStore::new(Connection::open(&file).expect("open"));
    s.conn.execute_batch("BEGIN").expect("begin");
    for rid in 1..=300 {
        s.put(&format!("r{rid}"), rid, "[\"g\"]", None, rid % 5);
    }
    s.conn.execute_batch("COMMIT").expect("commit");
    let memo = DocFreqMemo::new();
    let t = vec!["x".to_owned()];
    let first = {
        let src = StoreDocFreq::over(&s.conn, &file, "id");
        docfreq_store::ranking_statistics(&src, &memo, &t, &mut |_, _, _| true).expect("first")
    };
    assert!(first.is_some());
    for rid in [3, 301, 302] {
        s.put(&format!("r{rid}"), rid, "[\"h\"]", None, 2);
    }
    let threads = 8;
    let advanced = (Mutex::new(0), Condvar::new());
    let answers: Vec<_> = std::thread::scope(|scope| {
        let handles: Vec<_> = (0..threads)
            .map(|_| {
                let (memo, t, file, advanced) = (&memo, &t, &file, &advanced);
                scope.spawn(move || {
                    let conn = Connection::open(file).expect("open");
                    let src = Counted {
                        inner: StoreDocFreq::over(&conn, file, "id"),
                        advanced,
                        threads,
                    };
                    let h = [PyValue::str("h")];
                    let got = memo
                        .ranking_statistics(&src, t, &mut |s, _, _| s == h)
                        .expect("ranked");
                    (got, src.inner.warnings().len())
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|h| h.join().expect("thread"))
            .collect()
    });
    let took = *advanced.0.lock().expect("count");
    assert!(took > 1, "{took} threads took the advance");
    assert_eq!(took, threads, "each thread advanced the kept entry");
    for (got, warnings) in &answers {
        match got {
            Ranked::Found(found) => assert_eq!(found.0, 3),
            _ => panic!("no answer"),
        }
        assert_eq!(*warnings, 0);
    }
    let src = StoreDocFreq::over(&s.conn, &file, "id");
    let kept = memo.entry(&src.store_key()).expect("kept");
    assert_eq!(kept.inputs.version, 1, "advanced, not read again");
    assert_eq!(kept.inputs.head, src.memory_head().expect("head"));
    assert_eq!(kept.inputs.index.len(), 302);
}

/// Records a (row 1, scopes g, length 5), b (2, g, 7), c (3, h, 11), the
/// entry read at that head, and c moved into g.
fn three_and_c_moved() -> (HandStore, DocFreqInputs) {
    let mut s = hand();
    s.put("a", 1, "[\"g\"]", None, 5);
    s.put("b", 2, "[\"g\"]", None, 7);
    s.put("c", 3, "[\"h\"]", None, 11);
    let entry = {
        let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
        src.read_inputs(&src.memory_head().expect("head"))
            .expect("read")
    };
    s.put("c", 3, "[\"g\"]", None, 2);
    (s, entry)
}

fn advanced(s: &HandStore, entry: &DocFreqInputs) -> Result<Option<DocFreqInputs>, DocFreqError> {
    let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
    let head = src.memory_head().expect("head");
    src.advanced_inputs(entry, &head)
}

#[test]
fn an_entry_at_the_last_version_is_read_again() {
    // The reference's version is a Python int: it answers version 2**64.
    // The port holds a u64 and reads the entry again (None), in debug and
    // release alike, whose answers the advanced entry's equal.
    let (s, entry) = three_and_c_moved();
    let mut last = entry.clone();
    last.version = u64::MAX;
    assert_eq!(advanced(&s, &last), Ok(None));
    let mut before = entry;
    before.version = u64::MAX - 1;
    let got = advanced(&s, &before).expect("advance").expect("advanced");
    assert_eq!(got.version, u64::MAX);
}

/// One array of an entry shortened.
type Shorten = fn(&mut DocFreqInputs);

#[test]
fn an_entry_whose_arrays_disagree_raises_index_error() {
    // CPython 3.13.13, `_advanced_inputs` on such an entry: IndexError
    // from the list or array each case shortens.
    let (s, entry) = three_and_c_moved();
    let shortened: [(&str, Shorten); 4] = [
        ("length_of", |e| {
            e.length_of.as_mut().expect("lengths").pop();
        }),
        ("lengths", |e| {
            e.lengths.as_mut().expect("totals").pop();
        }),
        ("group_of", |e| {
            e.group_of.as_mut().expect("groups").pop();
        }),
        ("groups", |e| {
            e.groups.pop();
        }),
    ];
    for (name, shorten) in shortened {
        let mut e = entry.clone();
        shorten(&mut e);
        assert_eq!(advanced(&s, &e), Err(DocFreqError::IndexError), "{name}");
    }
    // No row moved: a new row's position is the index's length, past a
    // shorter `group_of` after its one append; a new group's total is
    // appended to a shorter `lengths`, then read past it.
    for (row, shorten) in [
        (
            "[\"g\"]",
            (|e| {
                e.group_of.as_mut().expect("groups").pop();
            }) as Shorten,
        ),
        ("[\"zeta\"]", |e| {
            e.lengths.as_mut().expect("totals").pop();
        }),
    ] {
        let mut s = hand();
        s.put("a", 1, "[\"g\"]", None, 5);
        s.put("b", 2, "[\"g\"]", None, 7);
        s.put("c", 3, "[\"h\"]", None, 11);
        let mut e = {
            let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
            src.read_inputs(&src.memory_head().expect("head"))
                .expect("read")
        };
        s.put("d", 4, row, None, 1);
        assert!(advanced(&s, &e).expect("advance").is_some(), "{row}");
        shorten(&mut e);
        assert_eq!(advanced(&s, &e), Err(DocFreqError::IndexError), "{row}");
    }
}

#[test]
fn numbers_past_64_bits_in_a_named_row() {
    let (mut s, entry) = three_and_c_moved();
    let big = PyInt::from_decimal("100000000000000000000").expect("an int");
    // A length past 64 bits on a row whose scopes are a list:
    // `array('q')` refuses it.
    s.conn
        .execute("UPDATE digest_records SET dl = 1e20 WHERE id = 'c'", [])
        .expect("planted");
    let overflow = Err(DocFreqError::Py(PyError::OverflowError));
    assert_eq!(advanced(&s, &entry), overflow);
    // On a row whose scopes are not a list it is not read.
    s.conn
        .execute(
            "UPDATE digest_records SET scopes = '\"g\"' WHERE id = 'c'",
            [],
        )
        .expect("planted");
    let got = advanced(&s, &entry).expect("advance").expect("advanced");
    assert_eq!(got.length_of, Some(vec![5, 7, 0]));
    // A row number past 64 bits is a new key at a new position; its old
    // position keeps its bit (the reference's own double count).
    s.conn
        .execute(
            "UPDATE digest_records SET scopes = '[\"g\"]', dl = 2, rid = 1e20 WHERE id = 'c'",
            [],
        )
        .expect("planted");
    let got = advanced(&s, &entry).expect("advance").expect("advanced");
    assert_eq!(got.index.get(&big), Some(&3));
    assert_eq!(got.changed, vec![(1, big.clone(), 3)]);
    assert_eq!(got.group_of, Some(vec![0, 0, 1, 0]));
    // Named again, the same int finds its position.
    s.home("c", Some("p"));
    let again = advanced(&s, &got).expect("advance").expect("advanced");
    assert_eq!(again.index.len(), 4);
    assert_eq!(again.changed.last(), Some(&(2, big, 3)));
}

#[test]
fn an_int_past_64_bits_orders_as_python_ints_do() {
    let int = |t: &str| PyInt::from_decimal(t).expect("an int");
    let mut ints = [
        int("100000000000000000000"),
        int("-100000000000000000000"),
        int("9223372036854775807"),
        int("-9223372036854775808"),
        int("0"),
        int("-18446744073709551617"),
        int("18446744073709551616"),
    ];
    ints.sort();
    let spelled: Vec<String> = ints.iter().map(ToString::to_string).collect();
    assert_eq!(
        spelled,
        [
            "-100000000000000000000",
            "-18446744073709551617",
            "-9223372036854775808",
            "0",
            "9223372036854775807",
            "18446744073709551616",
            "100000000000000000000",
        ]
    );
    assert_eq!(int("9223372036854775807"), PyInt::Small(i64::MAX));
    assert_eq!(int("-0009"), PyInt::Small(-9));
    assert_eq!(int("9223372036854775808").small(), None);
}

#[test]
fn a_refused_actor_comes_before_a_length_past_64_bits_on_one_row() {
    // CPython 3.13.13: on one row whose scopes are a list, a client
    // pydantic refuses and a length past 64 bits raise ValidationError
    // (the new group's Actor is made before `array('q')` takes the
    // length), in a full read and in the advance alike.
    let (s, entry) = three_and_c_moved();
    s.conn
        .execute(
            "UPDATE digest_records SET client = X'FF', dl = 1e20 WHERE id = 'c'",
            [],
        )
        .expect("planted");
    let refused = DocFreqError::Source("ValidationError".to_owned());
    assert_eq!(advanced(&s, &entry), Err(refused.clone()));
    let src = StoreDocFreq::over(&s.conn, Path::new(PATH), "id");
    let head = src.memory_head().expect("head");
    assert_eq!(src.read_inputs(&head).err(), Some(refused));
}
