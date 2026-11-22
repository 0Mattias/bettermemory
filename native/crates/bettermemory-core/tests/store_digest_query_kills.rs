//! Cases a plausible wrong `store::digest_query` fails: the asked hash
//! over the expanded tokens, the order of the early answers, where the
//! bound floor raises, the containment bound's rounding up, the claim
//! size, the cached question under one mark, and the memo's rules.

use std::cell::Cell;
use std::collections::BTreeSet;

use bettermemory_core::store::digest::DIGEST_SCHEMA;
use bettermemory_core::store::digest_query::{
    asked, digest_candidates, digest_shares, DigestMemo, QueryError, MEMO_ENTRIES,
};
use rusqlite::{params, Connection};
use sha2::{Digest, Sha256};

fn set(words: &[&str]) -> BTreeSet<String> {
    words.iter().map(|w| w.to_string()).collect()
}

fn words(range: std::ops::Range<usize>) -> Vec<String> {
    range.map(|i| format!("w{i}")).collect()
}

fn store() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(DIGEST_SCHEMA).unwrap();
    conn
}

fn put(conn: &Connection, doc: i64, id: &str, claim: i64, tokens: &[String]) {
    conn.execute(
        "INSERT INTO digest_records(doc, removed, id, n, claim, tokens) VALUES (?, 0, ?, ?, ?, ?)",
        params![doc, id, tokens.len() as i64, claim, tokens.join(" ")],
    )
    .unwrap();
    for t in tokens {
        conn.execute(
            "INSERT INTO digest_tokens(token, doc) VALUES (?, ?)",
            params![t, doc],
        )
        .unwrap();
    }
}

fn asked_row(conn: &Connection) -> Option<String> {
    conn.query_row("SELECT asked FROM temp.digest_asked", [], |r| r.get(0))
        .ok()
}

fn ids(conn: &Connection, tokens: &BTreeSet<String>, floor: f64, claims: bool) -> Vec<String> {
    let mut out: Vec<String> = digest_shares(conn, "S", Some("1:m"), tokens, false, floor, claims)
        .unwrap()
        .rows
        .unwrap()
        .into_iter()
        .map(|s| s.id)
        .collect();
    out.sort();
    out
}

#[test]
fn the_asked_hash_names_the_expanded_sorted_tokens() {
    let want = Sha256::digest(b"7:mac\nfeature\nfeature-flags\nflags\nzeta");
    let want: String = want.iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(asked("7:mac", &set(&["zeta", "feature-flags"])), want);
}

#[test]
fn early_answers_come_before_the_floor_is_read() {
    let conn = store();
    let none = digest_shares(
        &conn,
        "S",
        Some("1:m"),
        &BTreeSet::new(),
        false,
        f64::NAN,
        false,
    );
    assert!(none.unwrap().rows.is_none());
    let none = digest_shares(&conn, "S", None, &set(&["a"]), false, f64::NAN, false);
    assert!(none.unwrap().rows.is_none());
    assert!(matches!(
        digest_shares(
            &conn,
            "S",
            Some("1:m"),
            &set(&["a"]),
            false,
            f64::NAN,
            false
        ),
        Err(QueryError::Value(_))
    ));
    assert!(matches!(
        digest_candidates(
            &conn,
            "S",
            Some("1:m"),
            &set(&["a"]),
            false,
            f64::INFINITY,
            false
        ),
        Err(QueryError::Overflow(_))
    ));
    // Raised before `_share`: nothing was asked.
    assert_eq!(asked_row(&conn), None);
    // Past an SQLite INTEGER, it raises as it binds: after `_share`.
    assert!(matches!(
        digest_shares(&conn, "S", Some("1:m"), &set(&["a"]), false, 1e16, false),
        Err(QueryError::Overflow(_))
    ));
    assert!(asked_row(&conn).is_some());
    // -2**63 binds.
    let low = -9_223_372_036_854_775.808_f64;
    assert!(digest_shares(&conn, "S", Some("1:m"), &set(&["a"]), false, low, false).is_ok());
}

#[test]
fn containment_needs_four_shared_tokens_rounded_up() {
    let conn = store();
    let q = words(0..10);
    let mut four = words(0..4);
    four.extend(words(100..116));
    let mut three = words(0..3);
    three.extend(words(200..217));
    put(&conn, 1, "four", 20, &four);
    put(&conn, 2, "three", 20, &three);
    let q: BTreeSet<String> = q.into_iter().collect();
    assert_eq!(ids(&conn, &q, 1.0, false), vec!["four"]);
}

#[test]
fn the_floor_is_truncated_not_rounded() {
    let conn = store();
    let mut big = words(0..1);
    big.extend(words(1000..1599));
    put(&conn, 1, "big", 600, &big);
    let q = set(&["w0"]);
    // int(0.0015 * 1000) = 1: 1 * 1000 >= 1 * 600.
    assert_eq!(ids(&conn, &q, 0.0015, false), vec!["big"]);
    assert!(ids(&conn, &q, 0.002, false).is_empty());
}

#[test]
fn claims_size_by_the_claim_and_drop_claimless_records() {
    let conn = store();
    put(&conn, 1, "plain", 0, &words(0..3));
    put(&conn, 2, "claim", 3, &words(0..3));
    let q: BTreeSet<String> = words(0..3).into_iter().collect();
    assert_eq!(ids(&conn, &q, 0.5, false), vec!["claim", "plain"]);
    assert_eq!(ids(&conn, &q, 0.5, true), vec!["claim"]);
}

#[test]
fn one_mark_one_question_is_counted_once() {
    let conn = store();
    put(&conn, 1, "a", 0, &words(0..3));
    let q: BTreeSet<String> = words(0..3).into_iter().collect();
    assert_eq!(ids(&conn, &q, 0.5, false), vec!["a"]);
    put(&conn, 2, "b", 0, &words(0..3));
    assert_eq!(ids(&conn, &q, 0.5, false), vec!["a"]);
    let fresh = digest_shares(&conn, "S", Some("2:m"), &q, false, 0.5, false).unwrap();
    assert_eq!(fresh.rows.unwrap().len(), 2);
}

#[test]
fn a_failed_read_warns_and_answers_none() {
    let conn = store();
    conn.execute_batch("DROP TABLE digest_tokens").unwrap();
    let a = digest_candidates(
        &conn,
        "/x/s.db",
        Some("1:m"),
        &set(&["a"]),
        false,
        0.4,
        false,
    )
    .unwrap();
    assert!(a.rows.is_none());
    let w = a.warning.unwrap();
    assert!(
        w.starts_with("store /x/s.db: the digest could not be read ("),
        "{w}"
    );
    assert!(w.contains("digest_tokens"), "{w}");
}

#[test]
fn the_memo_keeps_answers_under_one_mark() {
    let mut memo: DigestMemo<u32, u32> = DigestMemo::new();
    let calls = Cell::new(0);
    let ask = |memo: &mut DigestMemo<u32, u32>, mark: Option<&str>, key: u32| {
        memo.get_or(mark, key, || {
            calls.set(calls.get() + 1);
            Some(key * 10)
        })
    };
    assert_eq!(ask(&mut memo, None, 1), Some(10));
    assert_eq!(ask(&mut memo, None, 1), Some(10));
    assert_eq!(calls.get(), 2);
    assert_eq!(memo.keys().count(), 0);
    ask(&mut memo, Some("a"), 1);
    ask(&mut memo, Some("a"), 1);
    assert_eq!(calls.get(), 3);
    ask(&mut memo, Some("b"), 1);
    assert_eq!(calls.get(), 4);
    for k in 2..=MEMO_ENTRIES as u32 {
        ask(&mut memo, Some("b"), k);
    }
    // A hit moves 1 to the end; the next new key drops 2, the oldest.
    ask(&mut memo, Some("b"), 1);
    ask(&mut memo, Some("b"), 99);
    let keys: Vec<u32> = memo.keys().copied().collect();
    assert_eq!(keys.len(), MEMO_ENTRIES);
    assert_eq!(keys[0], 3);
    assert_eq!(&keys[MEMO_ENTRIES - 2..], &[1, 99]);
    // An answer of None is not kept.
    assert_eq!(memo.get_or(Some("b"), 500, || None), None);
    assert!(!memo.keys().any(|&k| k == 500));
    assert_eq!(memo.keys().count(), MEMO_ENTRIES);
}

fn raw_row(conn: &Connection, id_sql: &str, n_sql: &str, stamped_sql: &str) {
    conn.execute_batch(&format!(
        "INSERT INTO digest_records(doc, removed, id, n, claim, stamped, tokens) \
         VALUES (1, 0, {id_sql}, {n_sql}, 1, {stamped_sql}, 'w0');\
         INSERT INTO digest_tokens(token, doc) VALUES ('w0', 1);"
    ))
    .unwrap();
}

#[test]
fn a_stamped_text_not_utf8_is_a_warning() {
    // The reference: sqlite3 fails to decode at fetch, OperationalError,
    // so None and a warning.
    let conn = store();
    raw_row(&conn, "'a'", "1", "CAST(x'ff' AS TEXT)");
    let got = digest_shares(&conn, "S", Some("1:m"), &set(&["w0"]), false, 0.0, false).unwrap();
    assert!(got.rows.is_none());
    assert!(got.warning.is_some());
}

#[test]
fn a_stamped_real_zero_is_false() {
    // stamped has INTEGER affinity, so SQLite stores a 0.0 as the integer 0
    // and only a fractional real (0.5) reaches the Real branch as a real.
    let conn = store();
    raw_row(&conn, "'a'", "1", "0.0");
    let got = digest_shares(&conn, "S", Some("1:m"), &set(&["w0"]), false, 0.0, false).unwrap();
    assert!(!got.rows.unwrap()[0].stamped);
    let conn = store();
    raw_row(&conn, "'a'", "1", "0.5");
    let got = digest_shares(&conn, "S", Some("1:m"), &set(&["w0"]), false, 0.0, false).unwrap();
    assert!(got.rows.unwrap()[0].stamped);
}

#[test]
fn a_blob_id_reads_as_repr_bytes() {
    // str(b'a\tb'), str(b"a'b"), str(b'a\\b\n\r\x00').
    for (sql, want) in [
        ("x'610962'", "b'a\\tb'"),
        ("x'612762'", "b\"a'b\""),
        ("x'6127220a'", "b'a\\'\"\\n'"),
        ("x'615c620d00'", "b'a\\\\b\\r\\x00'"),
    ] {
        let conn = store();
        raw_row(&conn, sql, "1", "0");
        let got = digest_shares(&conn, "S", Some("1:m"), &set(&["w0"]), false, 0.0, false).unwrap();
        assert_eq!(got.rows.unwrap()[0].id, want, "{sql}");
    }
}

#[test]
fn n_counts_the_raw_tokens_not_the_expanded() {
    // Raw n is 1, expanded 3: 1000 >= 500 * max(1, 1) admits it; over
    // the expanded count 1000 < 500 * 3 would not.
    let conn = store();
    put(&conn, 1, "a", 1, &["feature-flags".to_string()]);
    assert_eq!(ids(&conn, &set(&["feature-flags"]), 0.5, false), vec!["a"]);
}

fn shares(
    conn: &Connection,
) -> Result<Vec<bettermemory_core::store::digest_query::DigestShare>, QueryError> {
    digest_shares(conn, "S", Some("1:m"), &set(&["w0"]), false, 0.0, false).map(|a| a.rows.unwrap())
}

#[test]
fn n_reads_as_python_int_reads_it() {
    use bettermemory_core::pyvalue::PyValue;
    let int = |t: &str| PyValue::Int(t.into());
    // int(1e300) and int(2.5e19): exact, past 64 bits; int('٧ '),
    // int(' -1_2 ') and int(b'9 ') read numerals; int(2.5) truncates.
    for (sql, want) in [
        (
            "1e300",
            "1000000000000000052504760255204420248704468581108159154915854115511802457988908195786371375080447864043704443832883878176942523235360430575644792184786706982848387200926575803737830233794788090059368953234970799945081119038967640880074652742780142494579258788820056842838115669472196386865459400540160",
        ),
        ("2.5e19", "25000000000000000000"),
        ("'\u{0667} '", "7"),
        ("' -1_2 '", "-12"),
        ("x'3920'", "9"),
        ("2.5", "2"),
    ] {
        let conn = store();
        raw_row(&conn, "'a'", sql, "0");
        assert_eq!(shares(&conn).unwrap()[0].n, int(want), "{sql}");
    }
}

#[test]
fn n_python_int_refuses_raises_past_the_query() {
    // int('x'), int('1__2'), int(b'\xd9\xa7') (bytes take ASCII digits
    // only): ValueError; int(inf) of claim: OverflowError. None leave as warnings.
    for (sql, want) in [
        ("'x'", "invalid literal for int() with base 10: 'x'"),
        ("'1__2'", "invalid literal for int() with base 10: '1__2'"),
        (
            "x'd9a7'",
            "invalid literal for int() with base 10: b'\\xd9\\xa7'",
        ),
    ] {
        let conn = store();
        raw_row(&conn, "'a'", sql, "0");
        assert_eq!(
            shares(&conn).unwrap_err(),
            QueryError::Value(want.into()),
            "{sql}"
        );
    }
    let conn = store();
    raw_row(&conn, "'a'", "1", "0");
    // An infinite n fails the bound (0 * inf is NaN); claim is read too.
    conn.execute("UPDATE digest_records SET claim = 1e999", [])
        .unwrap();
    assert!(matches!(shares(&conn), Err(QueryError::Overflow(_))));
}

#[test]
fn a_fetch_error_comes_before_a_conversion_error() {
    // fetchall() decodes every row inside the try before int() runs: a
    // stamped text not UTF-8 is the warning even with a text n.
    let conn = store();
    raw_row(&conn, "'a'", "'x'", "CAST(x'ff' AS TEXT)");
    let got = digest_shares(&conn, "S", Some("1:m"), &set(&["w0"]), false, 0.0, false).unwrap();
    assert!(got.rows.is_none());
    assert!(got.warning.is_some());
}
