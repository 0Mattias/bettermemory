//! Hostile input through `pyre::regex`: long texts, a scan of a million
//! empty matches, integer bounds of the window, pathological patterns and
//! deep nesting, without a panic, and the scans linear in the text.

mod timing;

use bettermemory_core::pyre::regex::{CallError, Item, Regex, RegexError};

fn cp(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn re(p: &str) -> Regex {
    Regex::new_str(p, 0).unwrap_or_else(|e| panic!("{p}: {e:?}"))
}

/// An empty pattern matches at every position: `len + 1` matches, each
/// attempt after one fails in one step and the scan moves on.
#[test]
fn a_scan_of_empty_matches_ends() {
    let t = vec![97u32; 200_000];
    let found = re("").finditer(t.as_slice(), 0, isize::MAX).expect("str");
    assert_eq!(found.len(), t.len() + 1);
    assert_eq!(found.last().map(|m| m.regs[0]), Some((200_000, 200_000)));
    // `a*?` alternates an empty match and one `a`.
    let found = re("a*?").findall(t.as_slice(), 0, isize::MAX).expect("str");
    assert_eq!(found.len(), 2 * t.len() + 1);
}

#[test]
fn every_window_bound_is_answered() {
    let t = cp("abc");
    let r = re("b*");
    for pos in [isize::MIN, -1, 0, 1, 3, 4, isize::MAX] {
        for endpos in [isize::MIN, -1, 0, 2, 3, isize::MAX] {
            for answer in [
                r.r#match(t.as_slice(), pos, endpos).map(|m| m.is_some()),
                r.fullmatch(t.as_slice(), pos, endpos).map(|m| m.is_some()),
                r.search(t.as_slice(), pos, endpos).map(|m| m.is_some()),
            ] {
                assert!(answer.is_ok());
            }
            let n = r.finditer(t.as_slice(), pos, endpos).expect("str").len();
            assert_eq!(n, r.findall(t.as_slice(), pos, endpos).expect("str").len());
        }
    }
}

/// A backtracking blow-up is cut by the budget of `try_*`, a declared
/// refusal, and never by a panic.
#[test]
fn a_pathological_pattern_is_bounded_by_its_budget() {
    let r = re("(?:a|aa)*c");
    let t = vec![97u32; 60];
    assert_eq!(
        r.try_findall(t.as_slice(), 0, isize::MAX, Some(1_000_000))
            .map(|v| v.len()),
        Err(CallError::Budget)
    );
}

/// Nesting past the matcher's bound, and a group per position, are refused
/// as errors.
#[test]
fn deep_nesting_is_an_error() {
    let deep = format!("{}a{}", "(?:".repeat(5_000), ")".repeat(5_000));
    assert!(matches!(
        Regex::new_str(&deep, 0),
        Err(RegexError::Build(_))
    ));
    let wide = "(a)".repeat(3_000);
    match Regex::new_str(&wide, 0) {
        Ok(r) => {
            let t = vec![97u32; 3_000];
            let found = r.findall(t.as_slice(), 0, isize::MAX).expect("str");
            assert!(matches!(found.as_slice(), [Item::Tuple(g)] if g.len() == 3_000));
        }
        Err(e) => assert!(matches!(e, RegexError::Build(_) | RegexError::Lower(_))),
    }
}

/// `findall` over a text of many matches, and of many empty matches, grows
/// with the text.
#[test]
fn scans_grow_linearly() {
    let r = re("a*?");
    let small = cp(&"ab".repeat(5_000));
    let large = cp(&"ab".repeat(50_000));
    let time = |t: &Vec<u32>| {
        let r = &r;
        timing::once(|| r.findall(t.as_slice(), 0, isize::MAX).map(|v| v.len()))
    };
    timing::grows_within("a*? findall", 30, &mut || time(&small), &mut || {
        time(&large)
    });
    let r = re("[0-9a-f]{4,64}");
    let small = cp(&"12345678 z".repeat(2_000));
    let large = cp(&"12345678 z".repeat(20_000));
    let time = |t: &Vec<u32>| {
        let r = &r;
        timing::once(|| r.finditer(t.as_slice(), 0, isize::MAX).map(|v| v.len()))
    };
    timing::grows_within("hex finditer", 30, &mut || time(&small), &mut || {
        time(&large)
    });
}
