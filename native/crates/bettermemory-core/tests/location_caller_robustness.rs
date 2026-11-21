//! Hostile input for `location_caller`: tables far past their caps, very
//! long names and errors, many sessions forgotten at once. No panic, and
//! time that grows with the input as the reference's does.

mod location_caller_support;
mod timing;

use bettermemory_core::location_caller::{with_file_errors, Locks, Stands, LOCKS_CAP, STANDS_CAP};
use location_caller_support::{loc, res, text};
use std::time::Duration;

#[test]
fn every_lock_in_use_lets_the_table_grow_past_its_cap() {
    let locks = Locks::new();
    let held: Vec<_> = (0..LOCKS_CAP * 2)
        .map(|i| locks.take(&text(&format!("h{i}"))))
        .collect();
    assert_eq!(locks.snapshot().len(), LOCKS_CAP * 2);
    for t in held {
        locks.give_back(t);
    }
    locks.with_location_lock(&loc(&text("n"), None, None), || ());
    assert_eq!(locks.snapshot().len(), 1);
}

#[test]
fn a_megabyte_name_and_error() {
    let big = vec![0x0Au32; 1 << 20];
    let locks = Locks::new();
    locks.with_location_lock(&loc(&big, None, None), || ());
    let got = with_file_errors(&res(None, vec![]), &[big.clone(), big]);
    assert_eq!(got.lines.len(), 2);
    assert_eq!(got.lines[0].len(), 2 << 20);
    assert_eq!(got.lines[0][..2], [u32::from(b'\\'), u32::from(b'n')]);
}

#[test]
fn stands_far_past_the_cap_stay_at_it() {
    let st = Stands::new();
    let w = loc(&text("/w"), None, None);
    let c = res(Some(text("c")), vec![]);
    for i in 0..STANDS_CAP * 3 {
        st.keep_stand(&text("S"), Some(&text(&format!("s{i}"))), &w, &c);
    }
    assert_eq!(st.keys().len(), STANDS_CAP);
    st.forget_session(&text("nobody"));
    st.clear();
    assert!(st.keys().is_empty());
}

#[test]
fn errors_grow_no_worse_than_the_reference() {
    // The reference tests each error against the resolution's lines: the
    // time is lines times errors. Ten times the errors over the same
    // lines takes about ten times as long.
    let lines: Vec<_> = (0..200).map(|i| text(&format!("l{i}"))).collect();
    let r = res(None, lines);
    let errs = |n: usize| -> Vec<_> { (0..n).map(|i| text(&format!("e{i}"))).collect() };
    let (small, large) = (errs(200), errs(2000));
    let time = |e: &Vec<Vec<u32>>| -> Duration {
        timing::once(|| {
            std::hint::black_box(with_file_errors(&r, e));
        })
    };
    timing::grows_within("with_file_errors", 20, &mut || time(&small), &mut || {
        time(&large)
    });
}
