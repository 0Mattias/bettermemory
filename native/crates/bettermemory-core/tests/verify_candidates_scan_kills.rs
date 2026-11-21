//! Cases a plausible wrong scan or comparison form fails. Each names the
//! wrong implementation it kills. The answers are the reference's
//! (CPython 3.13.13, `PosixPath`). On a target that is not Unix both
//! functions decline, so these cases run on Unix only; the decline is
//! tested in `verify_candidates_scan_not_posix.rs`.

#![cfg(unix)]

use bettermemory_core::verify_candidates::scan::{
    extract_candidates, getpwnam_refuses, normalize_for_compare, scan_candidates, Candidate,
    CompareError, HomeError, ScanError, UserHome,
};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn s(c: &[u32]) -> String {
    c.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

fn none(_: &[u32]) -> UserHome {
    UserHome::Unknown
}

fn scan(body: &str) -> Vec<(String, bool, bool)> {
    let home = cps("/home/u");
    scan_candidates(&cps(body), Some(&home), &mut none)
        .unwrap()
        .into_iter()
        .map(|(p, a, sp): Candidate| (s(&p), a, sp))
        .collect()
}

fn alice(n: &[u32]) -> UserHome {
    if getpwnam_refuses(n) {
        UserHome::Refused
    } else if n == cps("alice") {
        UserHome::Found(cps("/home/alice/"))
    } else {
        UserHome::Unknown
    }
}

fn nfc(raw: &str, home: Option<&str>) -> Result<String, CompareError> {
    let home = home.map(cps);
    normalize_for_compare(&cps(raw), home.as_deref(), &mut alice).map(|r| s(&r))
}

#[test]
fn a_name_getpwnam_refuses_answers_the_raw_text() {
    // Kills a lookup that cannot say "refused" and so raises RuntimeError.
    assert_eq!(nfc("~a\0b/x", Some("/h")), Ok("~a\0b/x".to_owned()));
    assert_eq!(nfc("./~a\0b//x", Some("/h")), Ok("./~a\0b//x".to_owned()));
    let home = cps("/h");
    for (name, refused) in [
        (0xD800, true),
        (0xDC7F, true),
        (0xDFFF, true),
        (0xDC80, false),
        (0xDCFF, false),
    ] {
        let raw = vec![0x7E, name, 0x2F, 0x78];
        assert_eq!(getpwnam_refuses(&raw[1..2]), refused, "{name:X}");
        let got = normalize_for_compare(&raw, Some(&home), &mut alice);
        let want = if refused {
            Ok(raw.clone())
        } else {
            Err(CompareError::Home(HomeError))
        };
        assert_eq!(got, want, "{name:X}");
    }
}

#[test]
fn two_leading_slashes_stay_and_three_collapse() {
    // Kills a parser that strips every leading slash run to one.
    assert_eq!(nfc("//a/b", None).unwrap(), "//a/b");
    assert_eq!(nfc("///a/b", None).unwrap(), "/a/b");
}

#[test]
fn dot_parts_go_and_dotdot_parts_stay() {
    // Kills a normalizer that resolves `..` as `os.path.normpath` does.
    assert_eq!(nfc("/a/./b/../c/", None).unwrap(), "/a/b/../c");
    assert_eq!(nfc("./", None).unwrap(), ".");
}

#[test]
fn a_tilde_after_a_dot_part_expands() {
    // Kills an expansion that reads the raw text's first code point.
    assert_eq!(nfc("./~alice/x", None).unwrap(), "/home/alice/x");
    assert_eq!(nfc("/~alice/x", None).unwrap(), "/~alice/x");
}

#[test]
fn an_unknown_home_is_an_error_not_the_raw_text() {
    // Kills a port that catches the RuntimeError as it catches OSError.
    assert_eq!(nfc("~/x", None), Err(CompareError::Home(HomeError)));
    assert_eq!(
        nfc("~bob/x", Some("/h")),
        Err(CompareError::Home(HomeError))
    );
    assert_eq!(nfc("~/x", Some("~h")), Err(CompareError::Home(HomeError)));
    assert_eq!(nfc("~/x", Some("")).unwrap(), "/x");
}

#[test]
fn a_root_home_keeps_one_slash() {
    // Kills `rstrip("/")` without the `or sep` fallback.
    assert_eq!(nfc("~", Some("///")).unwrap(), "/");
}

#[test]
fn the_cap_returns_inside_the_backtick_pass() {
    // Kills a cap checked only after both passes: the ninth backtick would
    // raise IndexError.
    let body = "`/a/b` `/a/c` `/a/d` `/a/e` `/a/f` `/a/g` `/a/h` `/a/i` `\\ \\ .`";
    assert_eq!(scan(body).len(), 8);
    let late = "/a/b `\\ \\ .`";
    let home = cps("/h");
    assert_eq!(
        scan_candidates(&cps(late), Some(&home), &mut none),
        Err(ScanError::Index(
            bettermemory_core::verify_candidates::validate::IndexError
        ))
    );
}

#[test]
fn ambiguity_only_downgrades() {
    // Kills a dedup that keeps the last flag or upgrades it.
    assert_eq!(scan("/o/a Big /o/a ok"), [("/o/a".into(), false, false)]);
    assert_eq!(scan("/o/a ok /o/a Big"), [("/o/a".into(), false, false)]);
    assert_eq!(scan("/o/a Big"), [("/o/a".into(), true, false)]);
}

#[test]
fn a_trimmed_period_is_not_ambiguous() {
    // Kills an ambiguity test on the tail alone.
    assert_eq!(scan("/o/a. Big"), [("/o/a".into(), false, false)]);
}

#[test]
fn dedup_keys_on_the_expanded_form() {
    // Kills a dedup on the spelling.
    assert_eq!(
        scan("`~/x/y` /home/u/x/y"),
        [("~/x/y".into(), false, false)]
    );
}

#[test]
fn routes_come_from_the_unmasked_body() {
    // Kills a route set read from the masked body.
    assert!(scan("`ex.com/api` /api/users").is_empty());
}

#[test]
fn masking_keeps_offsets() {
    // Kills masking that drops the span: the tail would be one code point
    // and the candidate not ambiguous.
    assert_eq!(scan("/o/a `q`"), [("/o/a".into(), true, false)]);
}

#[test]
fn extract_answers_as_scan() {
    let home = cps("/h");
    let body = cps("see /a/b and `~/c`");
    assert_eq!(
        extract_candidates(&body, Some(&home), &mut none),
        scan_candidates(&body, Some(&home), &mut none)
    );
    assert_eq!(extract_candidates(&[], None, &mut none), Ok(vec![]));
}
