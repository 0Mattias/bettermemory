//! Hostile sizes for the search pool's route through the document
//! frequency memo: a store of 100,000 rows with a request that admits one
//! row in 10,000 (the ranking read a batch at a time, the time growing
//! with the store and not faster), 2,001 rows the memo's inputs do not
//! hold (five statements of `_admitted_rows_read`), a memo shared by eight
//! threads each drawing pools on a connection of its own, and pool sizes
//! at the ends of `usize`. No panic in debug or release.

mod common;
mod pool_memo;
mod timing;

use std::path::Path;

use bettermemory_core::store::docfreq::DocFreqMemo;
use bettermemory_core::store::pool::{Filters, PoolReader, POOL_SIZE};
use bettermemory_core::store::{digest, StoreFile};
use pool_memo::{expected, store, Digest};

fn draw(
    file: &Path,
    memo: &DocFreqMemo,
    query: &str,
    scope: Option<&str>,
    size: usize,
) -> Vec<i64> {
    let opened = StoreFile::open(file).expect("open");
    let reader = PoolReader::with_memo(&opened, memo);
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

/// One copy in 10,000 holds `rare`.
fn sparse(k: i64) -> Vec<String> {
    vec![if k % 10_000 == 0 { "rare" } else { "common" }.to_owned()]
}

#[test]
fn a_narrow_request_on_100000_rows_grows_with_the_store() {
    let small = store(10_000, 1, &sparse, Digest::Gone);
    let large = store(100_000, 1, &sparse, Digest::Gone);
    for (s, rows) in [(&small, 1), (&large, 10)] {
        let want = expected(&s.file, Some("postgres"), "rare");
        assert_eq!(want.len(), rows);
        let memo = DocFreqMemo::new();
        assert_eq!(
            draw(&s.file, &memo, "postgres", Some("rare"), POOL_SIZE),
            want
        );
        // The first admitted row ends the read: the batches past it are
        // never read.
        assert_eq!(draw(&s.file, &memo, "postgres", Some("rare"), 1), want[..1]);
        assert_eq!(
            draw(&s.file, &memo, "", Some("rare"), POOL_SIZE),
            expected(&s.file, None, "rare")
        );
    }
    // A pool on a memo of its own each time: the inputs read, the share,
    // the ranking. By the wall clock: the rows are read from a file.
    timing::grows_within(
        "a narrow pool on 10,000 and 100,000 rows",
        timing::TENFOLD,
        &mut || {
            timing::wall(|| {
                draw(
                    &small.file,
                    &DocFreqMemo::new(),
                    "postgres",
                    Some("rare"),
                    1,
                )
            })
        },
        &mut || {
            timing::wall(|| {
                draw(
                    &large.file,
                    &DocFreqMemo::new(),
                    "postgres",
                    Some("rare"),
                    1,
                )
            })
        },
    );
}

#[test]
fn two_thousand_and_one_unknown_rows_are_read_in_five_statements() {
    let s = store(2001, 5000, &|k| vec![format!("s{}", k % 3)], Digest::Base);
    let memo = DocFreqMemo::new();
    let all = expected(&s.file, None, "s0");
    assert_eq!(all.len(), 667);
    assert_eq!(draw(&s.file, &memo, "", Some("s0"), 2001), all);
    let ranked = expected(&s.file, Some("postgres"), "s1");
    assert_eq!(draw(&s.file, &memo, "postgres", Some("s1"), 2001), ranked);
}

#[test]
fn eight_threads_share_one_memo() {
    let s = store(300, 1, &|k| vec![format!("s{}", k % 4)], Digest::Copies);
    let memo = DocFreqMemo::new();
    let wants: Vec<(Vec<i64>, Vec<i64>)> = (0..4)
        .map(|g| {
            let scope = format!("s{g}");
            (
                expected(&s.file, Some("postgres"), &scope),
                expected(&s.file, None, &scope),
            )
        })
        .collect();
    std::thread::scope(|scope| {
        for t in 0..8 {
            let (file, memo, wants) = (&s.file, &memo, &wants);
            scope.spawn(move || {
                for round in 0..20 {
                    let g = (t + round) % 4;
                    let name = format!("s{g}");
                    assert_eq!(draw(file, memo, "postgres", Some(&name), 1000), wants[g].0);
                    assert_eq!(draw(file, memo, "", Some(&name), 1000), wants[g].1);
                }
            });
        }
    });
    assert_eq!(memo.keys().len(), 1);
}

#[test]
fn pool_sizes_at_the_ends_of_usize() {
    let s = store(900, 1, &|k| vec![format!("s{}", k % 2)], Digest::Copies);
    let memo = DocFreqMemo::new();
    for query in ["postgres", "", "   ", "qxjhkmvz"] {
        assert!(draw(&s.file, &memo, query, None, 0).is_empty());
        let wanted = if query == "qxjhkmvz" {
            Vec::new()
        } else if query.trim().is_empty() {
            expected(&s.file, None, "s0")
        } else {
            expected(&s.file, Some(query), "s0")
        };
        assert_eq!(draw(&s.file, &memo, query, Some("s0"), usize::MAX), wanted);
    }
}
