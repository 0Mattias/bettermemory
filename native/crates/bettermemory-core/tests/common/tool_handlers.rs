//! A bundle for the tests of `tool_handlers`: a store that counts its
//! commits, a recorder that remembers which store it writes through, and
//! a test-only `store.retryable_code`.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use bettermemory_core::config::{config_from_data, Ctx, Guards, Source};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::session::{Effects, SessionSource, SessionState};
use bettermemory_core::tool_handlers::{
    BundleStore, Offload, RebindStore, SqliteFailure, StoreSource, ToolHandlers,
};

/// A store: an id and its commit counter, shared by its clones.
#[derive(Clone, Debug)]
pub struct TestStore {
    pub id: u32,
    pub commits: Arc<AtomicU64>,
}

impl TestStore {
    pub fn new(id: u32) -> TestStore {
        TestStore {
            id,
            commits: Arc::new(AtomicU64::new(7)),
        }
    }

    pub fn commit(&self) {
        self.commits.fetch_add(1, Ordering::SeqCst);
    }
}

impl BundleStore for TestStore {
    fn commits(&self) -> u64 {
        self.commits.load(Ordering::SeqCst)
    }

    fn same_store(a: &Self, b: &Self) -> bool {
        Arc::ptr_eq(&a.commits, &b.commits)
    }
}

/// A recorder: the id of the store it writes through.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TestRecorder(pub u32);

impl RebindStore<TestStore> for TestRecorder {
    fn with_store(&self, store: TestStore) -> Self {
        TestRecorder(store.id)
    }
}

/// A session source no test asks.
pub struct NoSessions;

impl SessionSource for NoSessions {
    fn for_request(&self, _key: Option<PyStr>, _fx: &mut dyn Effects) -> Arc<SessionState> {
        unreachable!("no test resolves a session")
    }
}

/// A source that resolves one fixed store.
pub struct Fixed(pub TestStore);

impl StoreSource<TestStore> for Fixed {
    fn for_request(&self, _ctx: Option<&dyn std::any::Any>) -> TestStore {
        self.0.clone()
    }
}

pub type Bundle = ToolHandlers<TestStore, TestRecorder, ()>;

/// The defaults' config.
pub fn config() -> bettermemory_core::config::Config {
    let mut guards = Guards::new();
    let mut ctx = Ctx::new(&mut guards);
    config_from_data(
        &mut bettermemory_core::toml::Table::new(),
        &Source::registry(),
        &mut ctx,
    )
    .expect("the defaults load")
}

pub fn bundle(
    store: TestStore,
    source: Option<Arc<dyn StoreSource<TestStore>>>,
    offload: Option<Offload<TestStore>>,
) -> Bundle {
    let recorder = TestRecorder(store.id);
    ToolHandlers::new(
        config(),
        store,
        Arc::new(NoSessions),
        recorder,
        (),
        source,
        offload,
    )
}

/// `store.retryable_code`, test-only: the low byte read against
/// SQLITE_BUSY, SQLITE_LOCKED and SQLITE_FULL.
pub fn retryable(exc: &SqliteFailure) -> Option<&'static str> {
    match exc.code? & 0xFF {
        5 | 6 => Some("store_busy"),
        13 => Some("store_full"),
        _ => None,
    }
}
