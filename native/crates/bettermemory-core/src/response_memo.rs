//! `_response.py`'s per-hit drift memo (`_DRIFT_MEMO`, `_DRIFT_TIMED_OUT`,
//! `_clear_drift_memo`, `_recall_drift`, `_remember_drift`) as a generic
//! bounded LRU. The key and value types, the one instance and its
//! registration with the cache clearing belong to the drift pass.
//!
//! The clock (`time.monotonic`) is the argument `now`.

use std::collections::{BTreeMap, HashMap};
use std::hash::Hash;
use std::sync::Mutex;

/// `_DRIFT_MEMO_CAP`: entries each of the two tables keeps.
pub const DRIFT_MEMO_CAP: usize = 5000;
/// `_TIMED_OUT_SECONDS`: how long a timed-out resolution is answered.
pub const TIMED_OUT_SECONDS: f64 = 60.0;

/// An insertion-ordered map with move-to-end, as `OrderedDict` is used.
struct Ordered<K, V> {
    map: HashMap<K, (u64, V)>,
    order: BTreeMap<u64, K>,
    next: u64,
}

impl<K: Eq + Hash + Clone, V> Ordered<K, V> {
    fn new() -> Self {
        Ordered {
            map: HashMap::new(),
            order: BTreeMap::new(),
            next: 0,
        }
    }

    fn stamp(&mut self) -> u64 {
        let s = self.next;
        self.next += 1;
        s
    }

    /// `d[key] = value`: a key already held keeps its place.
    fn set(&mut self, key: &K, value: V) {
        if let Some(slot) = self.map.get_mut(key) {
            slot.1 = value;
            return;
        }
        let s = self.stamp();
        self.order.insert(s, key.clone());
        self.map.insert(key.clone(), (s, value));
    }

    fn move_to_end(&mut self, key: &K) {
        let s = self.stamp();
        if let Some(slot) = self.map.get_mut(key) {
            self.order.remove(&slot.0);
            slot.0 = s;
            self.order.insert(s, key.clone());
        }
    }

    fn remove(&mut self, key: &K) {
        if let Some((s, _)) = self.map.remove(key) {
            self.order.remove(&s);
        }
    }

    fn evict_past(&mut self, cap: usize) {
        while self.map.len() > cap {
            let Some((_, key)) = self.order.pop_first() else {
                break;
            };
            self.map.remove(&key);
        }
    }

    fn keys(&self) -> Vec<K> {
        self.order.values().cloned().collect()
    }

    fn clear(&mut self) {
        self.map.clear();
        self.order.clear();
    }
}

struct Tables<K, V> {
    memo: Ordered<K, V>,
    timed_out: Ordered<K, (f64, V)>,
}

/// The memo and the timed-out memo behind one lock, so each
/// look-up-and-touch and each merge-and-evict is atomic.
pub struct DriftMemo<K, V> {
    cap: usize,
    tables: Mutex<Tables<K, V>>,
}

impl<K: Eq + Hash + Clone, V: Clone> Default for DriftMemo<K, V> {
    fn default() -> Self {
        Self::new()
    }
}

impl<K: Eq + Hash + Clone, V: Clone> DriftMemo<K, V> {
    /// A memo bounded at [`DRIFT_MEMO_CAP`].
    pub fn new() -> Self {
        Self::with_cap(DRIFT_MEMO_CAP)
    }

    /// A memo bounded at `cap` (the reference's cap patched, for tests).
    pub fn with_cap(cap: usize) -> Self {
        DriftMemo {
            cap,
            tables: Mutex::new(Tables {
                memo: Ordered::new(),
                timed_out: Ordered::new(),
            }),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Tables<K, V>> {
        self.tables.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// `_clear_drift_memo`.
    pub fn clear(&self) {
        let mut t = self.lock();
        t.memo.clear();
        t.timed_out.clear();
    }

    /// `_recall_drift` at the monotonic time `now`: the value held for
    /// `key` (a hit moves to the end), else a timed-out value kept less
    /// than [`TIMED_OUT_SECONDS`] ago (it keeps its place); a timed-out
    /// entry outside that window (a clock that went back included, and
    /// NaN) is removed.
    pub fn recall(&self, key: &K, now: f64) -> Option<V> {
        let mut t = self.lock();
        if let Some((_, v)) = t.memo.map.get(key) {
            let v = v.clone();
            t.memo.move_to_end(key);
            return Some(v);
        }
        if let Some((_, (kept, v))) = t.timed_out.map.get(key) {
            let age = now - *kept;
            if (0.0..TIMED_OUT_SECONDS).contains(&age) {
                return Some(v.clone());
            }
            t.timed_out.remove(key);
        }
        None
    }

    /// `_remember_drift` at the monotonic time `now`: the resolutions set
    /// in order (a key already held keeps its place), each timed-out one
    /// kept with `now` and moved to the end, then the oldest entries of
    /// each table evicted past the cap.
    pub fn remember(&self, resolved: &[(K, V)], timed_out: &[(K, V)], now: f64) {
        let mut t = self.lock();
        for (k, v) in resolved {
            t.memo.set(k, v.clone());
        }
        for (k, v) in timed_out {
            t.timed_out.set(k, (now, v.clone()));
            t.timed_out.move_to_end(k);
        }
        let cap = self.cap;
        t.memo.evict_past(cap);
        t.timed_out.evict_past(cap);
    }

    /// The memo's keys, least recently used first.
    pub fn memo_keys(&self) -> Vec<K> {
        self.lock().memo.keys()
    }

    /// The timed-out memo's keys, oldest first.
    pub fn timed_out_keys(&self) -> Vec<K> {
        self.lock().timed_out.keys()
    }
}
