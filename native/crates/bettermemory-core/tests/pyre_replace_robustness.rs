//! Hostile input through `pyre::replace`: a text of many empty matches,
//! the count at its integer bounds, a pathological pattern under a
//! budget, and `sub` and `split` linear in the text.

mod timing;

use bettermemory_core::pyre::regex::{CallError, Regex};
use bettermemory_core::pyre::replace::ReplaceError;

fn re(p: &str) -> Regex {
    Regex::new_str(p, 0).unwrap_or_else(|e| panic!("{p}: {e:?}"))
}

#[test]
fn many_empty_matches_are_replaced() {
    let t = vec![97u32; 200_000];
    let (out, n) = re("").subn(&[45], &t, 0).expect("str");
    assert_eq!(n, t.len() + 1);
    assert_eq!(out.len(), 2 * t.len() + 1);
    let parts = re("x*").split(&t, 0).expect("str");
    assert_eq!(parts.len(), t.len() + 2);
}

#[test]
fn counts_at_their_bounds() {
    let r = re("a");
    let t = vec![97u32; 1_000];
    for count in [isize::MIN, -1, 0, 1, 999, 1_000, 1_001, isize::MAX] {
        let (out, n) = r.subn(&[], &t, count).expect("str");
        let want = if count == 0 {
            1_000
        } else {
            count.clamp(0, 1_000) as usize
        };
        assert_eq!(n, want);
        assert_eq!(out.len(), 1_000 - want);
        assert_eq!(r.split(&t, count).expect("str").len(), want + 1);
    }
}

#[test]
fn a_pathological_pattern_is_bounded_by_its_budget() {
    let r = re("(?:a|aa)*c");
    let t = vec![97u32; 60];
    assert_eq!(
        r.try_subn(&[45], &t, 0, Some(1_000_000)),
        Err(ReplaceError::Call(CallError::Budget))
    );
    assert_eq!(
        r.try_split(&t, 0, Some(1_000_000)),
        Err(ReplaceError::Call(CallError::Budget))
    );
}

#[test]
fn sub_and_split_grow_linearly() {
    let r = re("a*?");
    let small: Vec<u32> = "ab".repeat(5_000).chars().map(u32::from).collect();
    let large: Vec<u32> = "ab".repeat(50_000).chars().map(u32::from).collect();
    let time = |t: &Vec<u32>| {
        let r = &r;
        timing::once(|| r.subn(&[45], t, 0).map(|v| v.1))
    };
    timing::grows_within("a*? subn", 30, &mut || time(&small), &mut || time(&large));
    let r = re("(b)|a");
    let time = |t: &Vec<u32>| {
        let r = &r;
        timing::once(|| r.split(t, 0).map(|v| v.len()))
    };
    timing::grows_within("(b)|a split", 30, &mut || time(&small), &mut || {
        time(&large)
    });
}
