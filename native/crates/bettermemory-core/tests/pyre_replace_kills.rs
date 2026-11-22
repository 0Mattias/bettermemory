//! Cases a plausible wrong `sub`, `subn` or `split` fails: the attempt
//! after an empty match, the count's bounds, unset groups in `split`,
//! `None` from a function, the template refusal and its order before the
//! type check, and the scan stopped at the count. Each answer is
//! `re`'s, as CPython 3.13.13 gives it.

use bettermemory_core::pyre::regex::{CallError, Regex};
use bettermemory_core::pyre::replace::ReplaceError;

fn cp(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn re(p: &str) -> Regex {
    Regex::new_str(p, 0).unwrap_or_else(|e| panic!("{p}: {e:?}"))
}

fn text(v: &[u32]) -> String {
    v.iter().filter_map(|&c| char::from_u32(c)).collect()
}

fn sub(p: &str, repl: &str, t: &str, count: isize) -> (String, usize) {
    let (out, n) = re(p).subn(&cp(repl), &cp(t), count).expect("sub");
    (text(&out), n)
}

fn split(p: &str, t: &str, maxsplit: isize) -> Vec<Option<String>> {
    re(p)
        .split(&cp(t), maxsplit)
        .expect("split")
        .iter()
        .map(|x| x.as_deref().map(text))
        .collect()
}

fn some(items: &[&str]) -> Vec<Option<String>> {
    items.iter().map(|s| Some((*s).to_string())).collect()
}

/// `re.sub('a*?', '-', 'aab')` is `'-----b-'`: the attempt after an empty
/// match backtracks to the `a`; a scan stepping one past loses it.
#[test]
fn the_attempt_after_an_empty_match_backtracks() {
    assert_eq!(sub("a*?", "-", "aab", 0), ("-----b-".into(), 6));
    assert_eq!(sub("x*", "-", "abc", 0), ("-a-b-c-".into(), 4));
    assert_eq!(sub("a*", "-", "baaac", 0), ("-b--c-".into(), 4));
    assert_eq!(split("a*?", "aab", 0), some(&["", "", "", "", "", "b", ""]));
    assert_eq!(split("", "abc", 0), some(&["", "a", "b", "c", ""]));
}

/// 0 is no bound, a negative count finds nothing, a count past the
/// matches finds them all.
#[test]
fn the_count_bounds() {
    assert_eq!(sub("a", "-", "aaaa", 0).1, 4);
    assert_eq!(sub("a", "-", "aaaa", 1), ("-aaa".into(), 1));
    assert_eq!(sub("a", "-", "aaaa", -1), ("aaaa".into(), 0));
    assert_eq!(sub("a", "-", "aaaa", isize::MIN), ("aaaa".into(), 0));
    assert_eq!(sub("a", "-", "aaaa", isize::MAX), ("----".into(), 4));
    assert_eq!(split("a", "aaaa", 1), some(&["", "aaa"]));
    assert_eq!(split("a", "aaaa", -1), some(&["aaaa"]));
    assert_eq!(split("x*", "abc", 1), some(&["", "abc"]));
}

/// A group that took no part is `None` in `split`, not `''`.
#[test]
fn split_holds_none_for_an_unset_group() {
    assert_eq!(
        split("(a)|b", "abc", 0),
        vec![
            Some("".into()),
            Some("a".into()),
            Some("".into()),
            None,
            Some("c".into())
        ]
    );
    assert_eq!(split("()", "ab", 0), some(&["", "", "a", "", "b", "", ""]));
}

/// A function's `None` adds nothing; its matches are over the whole text
/// with `pos` 0.
#[test]
fn a_function_answering_none_adds_nothing() {
    let r = re("a|");
    let t = cp("bab");
    let mut seen = Vec::new();
    let (out, n) = r
        .subn_with(
            |m| {
                seen.push((m.regs[0], m.pos, m.endpos));
                (m.regs[0].0 != m.regs[0].1).then(|| cp("X"))
            },
            &t,
            0,
        )
        .expect("sub");
    assert_eq!((text(&out), n), ("bXb".into(), 4));
    assert_eq!(
        seen,
        vec![
            ((0, 0), 0, 3),
            ((1, 2), 0, 3),
            ((2, 2), 0, 3),
            ((3, 3), 0, 3)
        ]
    );
}

/// A literal with a backslash is refused, on a text of either type, with
/// no match, and before the text's type is checked.
#[test]
fn a_backslash_is_a_template_refused() {
    let r = re("a");
    assert_eq!(r.sub(&cp("\\n"), &cp("ab"), 0), Err(ReplaceError::Template));
    assert_eq!(
        r.sub(&cp("x\\"), &cp("zz"), -1),
        Err(ReplaceError::Template)
    );
    assert_eq!(
        r.sub(b"\\1".as_slice(), b"a".as_slice(), 0),
        Err(ReplaceError::Template)
    );
    assert_eq!(
        r.sub(b"x".as_slice(), b"a".as_slice(), 0),
        Err(ReplaceError::Call(CallError::Type { str_pattern: true }))
    );
    // The type check comes before the count: `subn` with -1 still checks.
    assert_eq!(
        r.split(b"a".as_slice(), -1),
        Err(ReplaceError::Call(CallError::Type { str_pattern: true }))
    );
    // A slash is not a backslash.
    assert_eq!(sub("a", "/", "ab", 0), ("/b".into(), 1));
}

/// No search runs once the count is reached: the budget that the second
/// search would pass is not met with a count of 1.
#[test]
fn the_scan_stops_at_the_count() {
    let r = re("c|(?:a|aa)*b");
    let mut t = cp("c");
    t.extend(std::iter::repeat_n(97, 40));
    assert_eq!(
        r.try_subn(&cp("-"), &t, 1, Some(10_000)).map(|(_, n)| n),
        Ok(1)
    );
    assert_eq!(
        r.try_subn(&cp("-"), &t, 0, Some(10_000)),
        Err(ReplaceError::Call(CallError::Budget))
    );
    assert_eq!(r.try_split(&t, 1, Some(10_000)).map(|v| v.len()), Ok(2));
}

/// Bytes in, bytes out.
#[test]
fn bytes_patterns_replace_bytes() {
    let r = Regex::new_bytes(b"(a)(b)", 0).expect("bytes");
    let t = b"abab".to_vec();
    let out = r
        .sub_with(
            |m| m.group(2).map(|(a, b)| t[a as usize..b as usize].to_vec()),
            &t,
            0,
        )
        .expect("bytes");
    assert_eq!(out, b"bb");
}
