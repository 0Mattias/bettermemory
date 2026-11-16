//! Readers while a native writer writes: in one process, a writer thread
//! makes 300 writes while a reader thread opens the store again and
//! again (`StoreFile::open`), runs the native check
//! (`log_verify_loading`) and a native search pool (`PoolReader`) for a
//! word every write carries. No report is wrong: the check says ok (the
//! head may be one row behind) or that the store kept changing (the fold
//! check's rule for a store a writer holds); the pool holds a prefix of
//! the writes (the first k records, for some k) and never fewer than an
//! earlier read saw.

mod common;

use std::sync::atomic::{AtomicBool, Ordering};

use bettermemory_core::store::create;
use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::store::pool::{Filters, PoolReader};
use bettermemory_core::store::StoreFile;
use common::created::{fixed, fixture};
use common::opened;
use common::stores::scratch;
use common::written::{self, instant, ulid};

const WRITES: u64 = 300;

#[test]
fn readers_see_a_prefix_and_no_wrong_report() {
    let dir = scratch("write-readers");
    let c = dir.0.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let id = common::stores::store_id();
    let file = c.join("memory.sqlite");
    let done = AtomicBool::new(false);
    let (reads, changing) = std::thread::scope(|scope| {
        scope.spawn(|| {
            let mut store =
                open_for_writing(&file, &c.join("keys"), &opened::engine_provider()).expect("open");
            let mut clock = written::clock(&[instant("2001-02-03T10:00:00.000001Z")]);
            for k in 0..WRITES {
                let p = written::memory_payload(
                    &ulid(k, "01J"),
                    &format!("zebra record {k}"),
                    &["global"],
                    &[],
                );
                store
                    .mutate("memory_put", &p, Some("w"), &mut clock)
                    .expect("write");
            }
            store.close().expect("close");
            done.store(true, Ordering::SeqCst);
        });
        let reader = scope.spawn(|| {
            let (mut reads, mut changing, mut seen) = (0, 0, 0usize);
            loop {
                let last = done.load(Ordering::SeqCst);
                let store = StoreFile::open(&file).expect("a reader opens");
                match store.log_verify_loading(&mut written::loader(c.join("keys"), id.clone())) {
                    Ok(report) => assert_eq!(report.status, "ok", "{report:?}"),
                    Err(e) => {
                        let text = e.to_string();
                        assert!(text.contains("changing"), "a wrong report: {text}");
                        changing += 1;
                    }
                }
                let pool = PoolReader::new(&store);
                let found = pool
                    .resolve_search_pool("zebra", &Filters::default(), 10_000, None)
                    .expect("search");
                let mut ids: Vec<String> = found.ids().iter().map(|s| (*s).to_owned()).collect();
                ids.sort();
                let prefix: Vec<String> = (0..ids.len() as u64).map(|k| ulid(k, "01J")).collect();
                assert_eq!(ids, prefix, "not a prefix of the writes");
                assert!(ids.len() >= seen, "a later read saw fewer");
                seen = ids.len();
                reads += 1;
                if last {
                    assert_eq!(seen, WRITES as usize);
                    return (reads, changing);
                }
            }
        });
        reader.join().expect("reader")
    });
    eprintln!("{reads} reads under the writer, {changing} said the store kept changing");
    assert!(reads >= 2);
}
