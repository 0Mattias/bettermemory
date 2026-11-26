//! Cases a plausible wrong `scope_match` fails, each answer the
//! reference's (read from `scope_match.py` and checked against it), with
//! the wrong reading written beside it where it is short:
//!
//! - the ancestor test without the separator appended, so `/a/foo` drops
//!   `/a/foobar`'s sibling;
//! - the case probe asked of the home where the reference asks the
//!   declared root (in the spans and in the cover);
//! - an IGNORECASE match kept without the probe;
//! - `str.lower` in place of IGNORECASE, which neither folds the long s
//!   or a final sigma one to one nor keeps a lengthened `İ` in place;
//! - a prefix lowered before it is cut, where the reference cuts first;
//! - `foo.bak` read as a boundary, or `foo.` at the end read as none;
//! - the alias built for the home itself;
//! - a most_common tie broken by sort order or by the last counted;
//! - overlapping needles skipped;
//! - the home probed once per scope, or not at all for a re-cased root;
//! - the occurrence search given up after the first guard-rejected hit,
//!   or walked without the occurrences that overlap a rejected one;
//! - the spans' literal prefix taken past the code points `_sre` leaves
//!   uncased, or its walk without overlapping prefixes;
//! - a scope declared twice read twice.

#![cfg(unix)]

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::scope_match::{
    boundary_after, declared_root_covers, declared_root_spans, drop_ancestor_roots,
    find_root_occurrence, home_alias, ignorecase_starts, ignorecase_starts_by_engine, roots_of,
    trim_evidence, ScopeError, EVIDENCE_PADDING,
};

fn cps(t: &str) -> PyStr {
    t.chars().map(u32::from).collect()
}

const HOME: &str = "/hd/u";

/// Runs `f` with a probe answering True for `folds`; the answer and the
/// paths probed.
fn tabled<T>(
    folds: &[&str],
    f: impl FnOnce(&mut dyn FnMut(&[u32]) -> Result<bool, ScopeError>) -> T,
) -> (T, Vec<String>) {
    let mut log = Vec::new();
    let got = {
        let mut probe = |p: &[u32]| {
            let spelled: String = p.iter().map(|&c| char::from_u32(c).unwrap()).collect();
            let folds_here = folds.contains(&spelled.as_str());
            log.push(spelled);
            Ok::<bool, ScopeError>(folds_here)
        };
        f(&mut probe)
    };
    (got, log)
}

fn pairs(p: &[(&str, &str)]) -> Vec<(PyStr, PyStr)> {
    p.iter().map(|(a, b)| (cps(a), cps(b))).collect()
}

fn spans(body: &str, root: &str, folds: &[&str]) -> (Vec<(usize, usize)>, Vec<String>) {
    let home = cps(HOME);
    let (got, log) = tabled(folds, |p| {
        declared_root_spans(
            &cps(body),
            &[cps("projects:a")],
            &pairs(&[("projects:a", root)]),
            Some(&home),
            p,
        )
    });
    (got.unwrap(), log)
}

#[test]
fn the_ancestor_test_appends_the_separator() {
    let roots = pairs(&[("a", "/a/foo"), ("b", "/a/foobar"), ("c", "/a/foo-bar")]);
    let kept = drop_ancestor_roots(roots.clone()).unwrap();
    assert_eq!(kept, roots);
    // Without the separator `/a/foo` reads as both siblings' ancestor.
    let wrong: Vec<_> = roots
        .iter()
        .filter(|(s, r)| {
            !roots
                .iter()
                .any(|(t, o)| t != s && o != r && o.starts_with(r))
        })
        .collect();
    assert_ne!(wrong.len(), kept.len());
    // With it, a real ancestor goes, and `/` (folded to empty) is every
    // absolute root's.
    let kept = drop_ancestor_roots(pairs(&[("a", "/a/foo/"), ("b", "/a/foo/x")])).unwrap();
    assert_eq!(kept, pairs(&[("b", "/a/foo/x")]));
    let kept = drop_ancestor_roots(pairs(&[("a", "/"), ("b", "/x"), ("c", "/x")])).unwrap();
    assert_eq!(kept, pairs(&[("b", "/x"), ("c", "/x")]));
}

#[test]
fn the_cover_probes_the_declared_root_not_the_home() {
    let home = cps(HOME);
    let run = |folds: &[&str]| {
        tabled(folds, |p| {
            declared_root_covers(
                &cps("/P/c/x"),
                0,
                &cps("/p"),
                &[cps("/P/c")],
                false,
                Some(&home),
                p,
            )
        })
    };
    let (got, log) = run(&["/P/c"]);
    assert_eq!((got.unwrap(), log), (true, vec!["/P/c".to_owned()]));
    let (got, log) = run(&[HOME]);
    assert_eq!((got.unwrap(), log), (false, vec!["/P/c".to_owned()]));
    // The cited spelling re-cased against the declared root: the same
    // probe, asked again.
    let (got, log) = tabled(&["/p/c"], |p| {
        declared_root_covers(
            &cps("/P/C/x"),
            0,
            &cps("/P"),
            &[cps("/p/c")],
            false,
            Some(&home),
            p,
        )
    });
    assert_eq!(
        (got.unwrap(), log),
        (true, vec!["/p/c".to_owned(), "/p/c".to_owned()])
    );
}

#[test]
fn a_case_variant_span_needs_the_root_s_probe() {
    let body = "see /W/A/x and /w/A and /w/a";
    assert_eq!(
        spans(body, "/w/a", &[]),
        (vec![(24, 28)], vec!["/w/a".to_owned()])
    );
    assert_eq!(
        spans(body, "/w/a", &["/w/a"]),
        (vec![(4, 8), (15, 19), (24, 28)], vec!["/w/a".to_owned()])
    );
    // The home folding case says nothing of the root's volume.
    assert_eq!(spans(body, "/w/a", &[HOME]).0, vec![(24, 28)]);
}

#[test]
fn ignorecase_is_re_s_simple_folding_not_str_lower() {
    // The long s and a final sigma fold one to one under IGNORECASE, and
    // `str.lower` leaves them as they are.
    assert_eq!(spans("/w/\u{17f}rc", "/w/src", &["/w/src"]).0, vec![(0, 6)]);
    assert_eq!(
        spans(
            "/w/\u{3c2} /w/\u{3a3} /w/\u{3c3}",
            "/w/\u{3c3}",
            &["/w/\u{3c3}"]
        )
        .0,
        vec![(0, 4), (5, 9), (10, 14)]
    );
    // `İ` matches `i` in place; `str.lower` lengthens it and moves the rest.
    assert_eq!(
        spans("/w/\u{130}x /w/ix", "/w/ix", &["/w/ix"]).0,
        vec![(0, 5), (6, 11)]
    );
    // `ß` never matches `ss`, nor the reverse.
    assert_eq!(
        spans("/w/stra\u{df}e /w/strasse", "/w/strasse", &["/w/strasse"]),
        (vec![(10, 20)], vec![])
    );
}

#[test]
fn a_prefix_is_cut_before_it_is_lowered() {
    let home = cps(HOME);
    // `/İ/x`'s first three code points lowered are `/i̇/`, never `/i̇`.
    let (got, log) = tabled(&["/\u{130}/x"], |p| {
        declared_root_covers(
            &cps("/\u{130}/x"),
            0,
            &cps("/i\u{307}"),
            &[cps("/\u{130}/x")],
            false,
            Some(&home),
            p,
        )
    });
    assert_eq!((got.unwrap(), log.len()), (false, 0));
    // And the home alias of a path re-cased past ASCII.
    let dotted = cps("/\u{130}");
    let (got, log) = tabled(&["/\u{130}"], |p| {
        home_alias(&cps("/i\u{307}/x"), Some(&dotted), p)
    });
    assert_eq!((got.unwrap(), log.len()), (None, 0));
}

#[test]
fn a_dot_continues_a_segment_only_before_a_non_space() {
    let body = cps("see /a/foo.bak and /a/foo/x.py");
    assert_eq!(find_root_occurrence(&body, &cps("/a/foo")), Some((19, 25)));
    assert!(!boundary_after(&cps("foo.bak"), 3));
    assert!(boundary_after(&cps("foo."), 3));
    assert!(boundary_after(&cps("foo. x"), 3));
    assert!(!boundary_after(&cps("foo_"), 3));
    assert!(!boundary_after(&cps("foo\u{e9}"), 3));
    // The search goes on past a guard-rejected hit, and the code point
    // before must not be alphanumeric.
    assert_eq!(
        find_root_occurrence(&cps("x/a/foo /a/foobar /a/foo"), &cps("/a/foo")),
        Some((18, 24))
    );
    assert_eq!(find_root_occurrence(&cps("aaaa"), &cps("aa")), None);
    assert_eq!(find_root_occurrence(&cps("-aa"), &cps("aa")), Some((1, 3)));
}

#[test]
fn the_home_itself_has_no_alias() {
    let home = cps(HOME);
    let (got, log) = tabled(&[HOME], |p| home_alias(&cps(HOME), Some(&home), p));
    assert_eq!((got.unwrap(), log.len()), (None, 0));
    let (got, _) = tabled(&[], |p| home_alias(&cps("/hd/u/"), Some(&home), p));
    assert_eq!(got.unwrap(), Some(cps("~/")));
    let (got, log) = tabled(&[HOME], |p| home_alias(&cps("/HD/U/x"), Some(&home), p));
    assert_eq!(
        (got.unwrap(), log),
        (Some(cps("~/x")), vec![HOME.to_owned()])
    );
}

#[test]
fn a_most_common_tie_goes_to_the_first_counted() {
    let home = cps(HOME);
    let by = vec![
        (
            cps("projects:a"),
            vec![(cps("/w/b"), 2), (cps("/w/a"), 2), (cps("/w/c"), 1)],
        ),
        (
            cps("projects:z"),
            vec![(cps("/w/y"), 1), (cps("/w/x"), 3), (cps("/w/z"), 3)],
        ),
    ];
    let (got, _) = tabled(&[], |p| roots_of(&by, Some(&home), p));
    assert_eq!(
        got.unwrap(),
        pairs(&[("projects:a", "/w/b"), ("projects:z", "/w/x")])
    );
}

#[test]
fn overlapping_needles_each_count() {
    assert_eq!(spans("aaaa", "aa", &[]).0, vec![(0, 2), (1, 3), (2, 4)]);
    assert_eq!(
        spans("/x/x/x/x", "/x/x", &[]).0,
        vec![(0, 4), (2, 6), (4, 8)]
    );
}

#[test]
fn the_home_is_probed_once_and_only_for_a_re_cased_root() {
    let home = cps(HOME);
    let by = vec![
        (cps("projects:a"), vec![(cps("/HD/U"), 1)]),
        (cps("projects:b"), vec![(cps("/hd/U"), 1)]),
        (cps("projects:c"), vec![(cps("/w/c"), 1)]),
    ];
    let (got, log) = tabled(&[HOME], |p| roots_of(&by, Some(&home), p));
    assert_eq!(
        (got.unwrap(), log),
        (pairs(&[("projects:c", "/w/c")]), vec![HOME.to_owned()])
    );
    let (got, log) = tabled(&[], |p| roots_of(&by[2..], Some(&home), p));
    assert_eq!((got.unwrap().len(), log.len()), (1, 0));
    // No home: the RuntimeError comes first, even for no scopes.
    let (got, _) = tabled(&[], |p| roots_of(&[], None, p));
    assert!(matches!(got, Err(ScopeError::Home(_))));
}

#[test]
fn the_trim_cuts_and_marks_each_side() {
    let body = cps(&format!("{}/a/b{}", "x".repeat(30), "y".repeat(30)));
    let got = trim_evidence(&body, 30, 34, EVIDENCE_PADDING);
    assert_eq!(
        got,
        cps(&format!("...{}/a/b{}...", "x".repeat(24), "y".repeat(24)))
    );
    let got = trim_evidence(&cps("  a\n\n b \u{3000} c  "), 3, 4, EVIDENCE_PADDING);
    assert_eq!(got, cps("a b c"));
}

#[test]
fn an_occurrence_overlapping_a_rejected_one_is_walked() {
    // The occurrence at 1 follows `x`; the one at 3 overlaps it and stands.
    assert_eq!(
        find_root_occurrence(&cps("x/././ "), &cps("/./")),
        Some((3, 6))
    );
}

#[test]
fn a_scope_declared_twice_is_read_once() {
    let home = cps(HOME);
    let scope = cps("projects:a");
    let (got, probes) = tabled(&["/w/P"], |p| {
        declared_root_spans(
            &cps("see /W/p/x and /w/P"),
            &[scope.clone(), scope.clone()],
            &[(scope.clone(), cps("/w/P"))],
            Some(&home),
            p,
        )
    });
    assert_eq!(got.unwrap(), vec![(4, 8), (15, 19)]);
    assert_eq!(probes, vec!["/w/P".to_owned()]);
}

/// xorshift64*, as the sweep draws.
struct Rng(u64);

impl Rng {
    fn next(&mut self, bound: u64) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D) % bound
    }
}

/// Code points `_sre` leaves uncased (`/`, `~`, a digit, `.`, `-`, a
/// space, U+00AA, which has the property Cased) and cased ones whose
/// IGNORECASE classes reach past ASCII (the Kelvin sign, the long s, the
/// three sigmas, the dotted and dotless i, U+00DF).
const ALPHABET: [char; 21] = [
    '/', '~', '1', '.', '-', ' ', '\u{aa}', 'k', 'K', '\u{212a}', 's', 'S', '\u{17f}', '\u{3c3}',
    '\u{3a3}', '\u{3c2}', 'i', 'I', '\u{130}', '\u{131}', '\u{df}',
];

#[test]
fn the_prefix_walk_folds_as_the_engine_past_ascii() {
    let mut rng = Rng(0x005C_0BE1_EAF5);
    let pick = |rng: &mut Rng, n: u64| -> PyStr {
        (0..n)
            .map(|_| u32::from(ALPHABET[rng.next(ALPHABET.len() as u64) as usize]))
            .collect()
    };
    let mut walked = 0usize;
    for _ in 0..20_000 {
        let lead = rng.next(4);
        let mut needle: PyStr = (0..lead)
            .map(|_| u32::from(ALPHABET[rng.next(7) as usize]))
            .collect();
        let tail = rng.next(5) + 1;
        needle.extend(pick(&mut rng, tail));
        let len = rng.next(32);
        let hay = pick(&mut rng, len);
        let got = ignorecase_starts(&hay, &needle);
        assert_eq!(
            got,
            ignorecase_starts_by_engine(&hay, &needle),
            "{needle:?} in {hay:?}"
        );
        walked += got.map_or(0, |starts| starts.len());
    }
    assert!(walked > 1000, "{walked} matches");
}
