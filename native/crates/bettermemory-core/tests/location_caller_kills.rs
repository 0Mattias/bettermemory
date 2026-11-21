//! Cases a plausible wrong `location_caller` fails: eviction of a lock in
//! use or in the wrong order, a stand kept without a checkout or evicted
//! newest first, the machine kept after a failed open, a store recorded
//! before its record returned, errors deduplicated among themselves, and
//! the namespace test asking what it need not.

mod location_caller_support;

use bettermemory_core::location_caller::{
    cwd_of, lock_name, same_namespace, with_file_errors, Locks, ProcessMachine, Stands, LOCKS_CAP,
    STANDS_CAP,
};
use location_caller_support::{identity, loc, res, text};
use std::sync::{Arc, Barrier};
use std::thread;

#[test]
fn caps_are_four_times_the_cache() {
    assert_eq!(LOCKS_CAP, 1024);
    assert_eq!(STANDS_CAP, 1024);
}

#[test]
fn an_empty_root_names_the_cwd() {
    let l = loc(&text("/w"), Some(Vec::new()), None);
    assert_eq!(lock_name(&l), text("m:/w"));
}

#[test]
fn a_key_names_device_and_inode_not_birth() {
    let l = loc(
        &text("/w"),
        Some(text("/r")),
        Some((text("1"), text("2"), Some(text("3")))),
    );
    assert_eq!(lock_name(&l), text("m:1:2"));
}

#[test]
fn eviction_keeps_a_lock_taken_but_not_acquired() {
    let locks = Locks::new();
    let held = locks.take(&text("held"));
    for i in 0..LOCKS_CAP - 1 {
        locks.with_location_lock(&loc(&text(&format!("z{i}")), None, None), || ());
    }
    // Past the cap: the next new name drops every unused lock.
    let snap = locks.snapshot();
    assert_eq!(snap.len(), LOCKS_CAP);
    locks.with_location_lock(&loc(&text("new"), None, None), || ());
    let snap = locks.snapshot();
    assert_eq!(snap.len(), 2);
    assert_eq!(snap[0], (text("held"), 1));
    assert_eq!(snap[1], (text("m:new"), 0));
    locks.give_back(held);
    assert_eq!(locks.snapshot()[0], (text("held"), 0));
}

#[test]
fn a_known_name_past_the_cap_evicts_nothing() {
    let locks = Locks::new();
    for i in 0..LOCKS_CAP {
        locks.with_location_lock(&loc(&text(&format!("z{i}")), None, None), || ());
    }
    locks.with_location_lock(&loc(&text("z5"), None, None), || ());
    assert_eq!(locks.snapshot().len(), LOCKS_CAP);
}

#[test]
fn a_panic_inside_still_gives_the_lock_back() {
    let locks = Locks::new();
    let l = loc(&text("/w"), None, None);
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        locks.with_location_lock(&l, || panic!("inside"))
    }));
    assert!(r.is_err());
    assert_eq!(locks.snapshot(), vec![(text("m:/w"), 0)]);
    locks.with_location_lock(&l, || ());
}

#[test]
fn one_location_runs_one_at_a_time() {
    let locks = Arc::new(Locks::new());
    let inside = Arc::new(std::sync::Mutex::new((0u32, 0u32)));
    let start = Arc::new(Barrier::new(4));
    let threads: Vec<_> = (0..4)
        .map(|_| {
            let (locks, inside, start) = (locks.clone(), inside.clone(), start.clone());
            thread::spawn(move || {
                start.wait();
                for _ in 0..200 {
                    locks.with_location_lock(&loc(&text("/w"), None, None), || {
                        let mut g = inside.lock().unwrap();
                        g.0 += 1;
                        g.1 = g.1.max(g.0);
                        drop(g);
                        thread::yield_now();
                        inside.lock().unwrap().0 -= 1;
                    });
                }
            })
        })
        .collect();
    for t in threads {
        t.join().unwrap();
    }
    assert_eq!(inside.lock().unwrap().1, 1);
    assert_eq!(locks.snapshot(), vec![(text("m:/w"), 0)]);
}

#[test]
fn a_stand_without_checkout_drops_the_old_one() {
    let st = Stands::new();
    let w = loc(&text("/w"), None, None);
    st.keep_stand(
        &text("S"),
        Some(&text("a")),
        &w,
        &res(Some(text("c")), vec![]),
    );
    st.keep_stand(&text("S"), Some(&text("a")), &w, &res(None, vec![]));
    assert!(st.stand_of(&text("S"), Some(&text("a"))).is_none());
    // An empty checkout is a checkout.
    st.keep_stand(
        &text("S"),
        Some(&text("a")),
        &w,
        &res(Some(Vec::new()), vec![]),
    );
    assert!(st.stand_of(&text("S"), Some(&text("a"))).is_some());
}

#[test]
fn the_oldest_stand_goes_first_and_a_refresh_moves_to_the_end() {
    let st = Stands::new();
    let w = loc(&text("/w"), None, None);
    let c = res(Some(text("c")), vec![]);
    for i in 0..STANDS_CAP {
        st.keep_stand(&text("S"), Some(&text(&format!("s{i}"))), &w, &c);
    }
    st.keep_stand(&text("S"), Some(&text("s0")), &w, &c);
    st.keep_stand(&text("S"), Some(&text("n")), &w, &c);
    let keys = st.keys();
    assert_eq!(keys.len(), STANDS_CAP);
    assert_eq!(keys[0].1, text("s2"));
    assert_eq!(keys[STANDS_CAP - 2].1, text("s0"));
    assert!(st.stand_of(&text("S"), Some(&text("s1"))).is_none());
}

#[test]
fn forget_drops_the_session_in_every_store_only() {
    let st = Stands::new();
    let w = loc(&text("/w"), None, None);
    let c = res(Some(text("c")), vec![]);
    for (store, session) in [("S", "a"), ("T", "a"), ("S", "b")] {
        st.keep_stand(&text(store), Some(&text(session)), &w, &c);
    }
    st.forget_session(&text("a"));
    assert_eq!(st.keys(), vec![(text("S"), text("b"))]);
    assert!(st.stand_of(&text("S"), None).is_none());
}

#[test]
fn a_failed_open_is_not_kept() {
    let pm = ProcessMachine::new();
    assert!(pm.process_machine(|| Err::<_, ()>(())).is_err());
    let m = pm
        .process_machine(|| Ok::<_, ()>(identity(&text("A"))))
        .unwrap();
    assert_eq!(m.id, text("A"));
    let m = pm
        .process_machine(|| Ok::<_, ()>(identity(&text("B"))))
        .unwrap();
    assert_eq!(m.id, text("A"));
}

#[test]
fn a_failed_record_is_asked_again() {
    let pm = ProcessMachine::new();
    let a = identity(&text("A"));
    assert!(pm.recorded(&text("S"), &a, |_| Err(())).is_err());
    let mut calls = 0;
    pm.recorded::<()>(&text("S"), &a, |_| {
        calls += 1;
        Ok(())
    })
    .unwrap();
    pm.recorded::<()>(&text("S"), &a, |_| {
        calls += 1;
        Ok(())
    })
    .unwrap();
    assert_eq!(calls, 1);
    pm.clear();
    pm.recorded::<()>(&text("S"), &a, |_| {
        calls += 1;
        Ok(())
    })
    .unwrap();
    assert_eq!(calls, 2);
}

#[test]
fn equal_errors_both_go_in_and_lines_are_not_escaped() {
    let r = res(Some(text("c")), vec![text("a\nb")]);
    let got = with_file_errors(&r, &[text("x"), text("x"), text("a\nb")]);
    assert_eq!(
        got.lines,
        vec![text("a\nb"), text("x"), text("x"), text("a\\nb")]
    );
}

#[test]
fn cwd_of_asks_getcwd_only_without_a_cwd() {
    assert_eq!(cwd_of(Some(1), || panic!("asked")), Some(1));
    assert_eq!(cwd_of(None, || Some(2)), Some(2));
    assert_eq!(cwd_of::<u8>(None, || None), None);
}

#[test]
fn same_namespace_asks_only_what_it_needs() {
    let k = (text("1"), text("2"), None);
    assert!(!same_namespace(
        None,
        || panic!("key"),
        Some(&b"h"[..]),
        || panic!("host")
    ));
    assert!(same_namespace(
        Some(&k),
        || Some(k.clone()),
        None,
        || panic!("host")
    ));
    let born = (text("1"), text("2"), Some(text("5")));
    assert!(same_namespace(
        Some(&k),
        || Some(born.clone()),
        None,
        || None
    ));
    let other = (text("1"), text("2"), Some(text("6")));
    assert!(!same_namespace(Some(&born), || Some(other), None, || None));
    // An empty name the client gives never matches: `_hostname` answers
    // None for an empty one.
    assert!(!same_namespace(
        Some(&k),
        || Some(k.clone()),
        Some(&b""[..]),
        || None
    ));
    assert!(same_namespace(
        Some(&k),
        || Some(k.clone()),
        Some(&b"h"[..]),
        || Some(b"h".to_vec())
    ));
}
