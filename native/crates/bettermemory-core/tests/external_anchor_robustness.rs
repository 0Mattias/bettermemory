//! Real file limits, descriptor ownership and one-snapshot SQLite reads.
mod common;

use bettermemory_core::anchor::{self, Anchor, AnchorError};
use bettermemory_core::fsutil::{bounded_read, BoundedReadError};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;
use rusqlite::{
    hooks::{AuthAction, AuthContext, Authorization},
    Connection,
};

fn requested() -> Vec<(Vec<u32>, Anchor)> {
    vec![(
        cps("written"),
        Anchor {
            store_id: cps("mine"),
            seq: "2".into(),
            mac: cps("m"),
            chain_sha256: cps("wrong"),
            created_at: vec![],
        },
    )]
}
#[test]
fn owning_and_joined_errors_preserve_the_transaction() {
    for joined in [false, true] {
        for fail_at in [1usize, 2] {
            let mut conn = Connection::open_in_memory().unwrap();
            conn.execute_batch(
                "CREATE TABLE log(seq INTEGER PRIMARY KEY,mac TEXT);INSERT INTO log VALUES(2,'m')",
            )
            .unwrap();
            if joined {
                conn.execute_batch("BEGIN;INSERT INTO log VALUES(3,'pending')")
                    .unwrap();
            }
            let mut selects = 0;
            conn.authorizer(Some(move |ctx: AuthContext<'_>| {
                if matches!(ctx.action, AuthAction::Select) {
                    selects += 1;
                    if selects == fail_at {
                        return Authorization::Deny;
                    }
                }
                Authorization::Allow
            }))
            .unwrap();
            assert!(matches!(
                anchor::check_anchors(&conn, &cps("mine"), &requested()),
                Err(AnchorError::Store(_))
            ));
            assert_eq!(conn.is_autocommit(), !joined);
            if joined {
                assert_eq!(
                    conn.query_row("SELECT count(*) FROM log", [], |r| r.get::<_, i64>(0))
                        .unwrap(),
                    2
                );
                conn.execute_batch("ROLLBACK").unwrap();
            }
        }
    }
}
#[test]
fn wal_commit_between_digest_and_point_read_is_seen_on_next_check() {
    let tmp = common::stores::scratch("anchor-snapshot");
    let file = tmp.0.join("log.sqlite");
    let mut conn = Connection::open(&file).unwrap();
    conn.execute_batch("PRAGMA journal_mode=WAL;CREATE TABLE log(seq INTEGER PRIMARY KEY,mac TEXT);INSERT INTO log VALUES(1,'a');").unwrap();
    let writer = Connection::open(file).unwrap();
    let mut selects = 0;
    conn.authorizer(Some(move |ctx: AuthContext<'_>| {
        if matches!(ctx.action, AuthAction::Select) {
            selects += 1;
            if selects == 2 {
                writer.execute("INSERT INTO log VALUES(2,'m')", []).unwrap();
            }
        }
        Authorization::Allow
    }))
    .unwrap();
    let one = anchor::check_anchors(&conn, &cps("mine"), &requested()).unwrap();
    assert_eq!(one[0].get("reason"), Some(&PyValue::str("missing")));
    let next = anchor::check_anchors(&conn, &cps("mine"), &requested()).unwrap();
    assert_eq!(next[0].get("reason"), Some(&PyValue::str("digest")));
}
#[test]
fn bounded_regular_files_refuse_oversize_before_read_and_accept_exact_limit() {
    let root = common::stores::scratch("anchor-limit");
    let input = root.0.join("file");
    let f = std::fs::File::create(&input).unwrap();
    f.set_len(anchor::MAX_ANCHOR_FILE_BYTES + 1).unwrap();
    assert!(
        matches!(bounded_read(&input,anchor::MAX_ANCHOR_FILE_BYTES),Err(BoundedReadError::TooLarge{size,limit}) if size==limit+1)
    );
    f.set_len(512).unwrap();
    assert_eq!(bounded_read(&input, 512).unwrap(), vec![0; 512]);
    f.set_len(0).unwrap();
    assert!(bounded_read(&input, 0).unwrap().is_empty());
}
#[cfg(unix)]
#[test]
fn repeated_directory_refusals_release_the_open_file() {
    let root = common::stores::scratch("anchor-dir");
    let mut used = std::collections::BTreeSet::new();
    for _ in 0..48 {
        match bounded_read(&root.0, 100) {
            Err(BoundedReadError::Directory(fd)) => {
                used.insert(fd);
            }
            e => panic!("{e:?}"),
        }
    }
    // Other tests may hold a few concurrent descriptors. A leak would retain all 48.
    assert!(used.len() < 24, "{used:?}");
}
