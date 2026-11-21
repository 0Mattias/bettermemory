//! The registry of the crate's module-level caches (`_caches.py`).
//!
//! A module that keeps an exact cache registers the function that empties
//! it, and [`clear_all`] runs every registered function in the order they
//! were registered. No cache is registered here yet.

use std::sync::Mutex;

/// A registered clearer under its key.
type Entry = (&'static str, fn());

/// The registered clearers, oldest first.
static CLEARERS: Mutex<Vec<Entry>> = Mutex::new(Vec::new());

fn clearers() -> std::sync::MutexGuard<'static, Vec<Entry>> {
    CLEARERS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// `register`: adds `clear` under `key` unless an entry with that key is
/// already there. The key is the Rust spelling of Python's equality on the
/// clearer (`clear not in _CLEARERS`): two registrations of the same cache
/// pass the same key and are kept once. Function pointers are not compared,
/// since Rust does not guarantee that one function has one address.
pub fn register(key: &'static str, clear: fn()) {
    let mut list = clearers();
    if !list.iter().any(|(k, _)| *k == key) {
        list.push((key, clear));
    }
}

/// `clear_all`: runs every registered clearer in registration order, over
/// a copy of the list taken under the lock (`tuple(_CLEARERS)`), so a
/// clearer may itself register; one it adds runs on the next call.
pub fn clear_all() {
    let snapshot: Vec<fn()> = clearers().iter().map(|(_, c)| *c).collect();
    for clear in snapshot {
        clear();
    }
}

/// The registered keys, oldest first.
pub fn keys() -> Vec<&'static str> {
    clearers().iter().map(|(k, _)| *k).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static ORDER: Mutex<Vec<u8>> = Mutex::new(Vec::new());
    static RUNS: AtomicUsize = AtomicUsize::new(0);

    fn first() {
        ORDER.lock().unwrap().push(1);
    }
    fn second() {
        ORDER.lock().unwrap().push(2);
    }
    fn late() {
        RUNS.fetch_add(1, Ordering::SeqCst);
    }
    fn registers_late() {
        register("test.late", late);
    }

    // One test: the registry is process-wide state.
    #[test]
    fn dedup_order_and_reentrant_register() {
        register("test.first", first);
        register("test.second", second);
        register("test.first", second);
        let mine: Vec<_> = keys()
            .into_iter()
            .filter(|k| k.starts_with("test."))
            .collect();
        assert_eq!(mine, ["test.first", "test.second"]);
        clear_all();
        assert_eq!(*ORDER.lock().unwrap(), [1, 2]);

        register("test.reg", registers_late);
        clear_all();
        assert_eq!(RUNS.load(Ordering::SeqCst), 0, "added during the run");
        assert!(keys().contains(&"test.late"));
        clear_all();
        assert_eq!(RUNS.load(Ordering::SeqCst), 1);
        assert_eq!(*ORDER.lock().unwrap(), [1, 2, 1, 2, 1, 2]);
    }
}
