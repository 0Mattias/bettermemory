//! `claims`' leaves over hostile input: very long lines, long runs that a
//! backtracking reading of the patterns would retry, every code point, and
//! the digit limit's edge. No panic, and time linear in the input: ten
//! times the input costs under sixty times the time.

mod timing;

use std::time::{Duration, Instant};

use bettermemory_core::claims;

fn c(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// Ten times the input within sixty times the time, on the least times
/// seen (`timing`): time stolen by other work on the machine only ever
/// adds, and a quadratic path stays a hundred times slower in every round.
/// `make` builds an input once per size, outside the timed part, so the
/// claim is about the code under test and not about allocating a larger
/// input; `run` is given the input and its size.
fn linear<T>(name: &str, make: impl Fn(usize) -> T, run: impl Fn(&T, usize)) {
    run(&make(1_000), 1_000);
    let (small, large) = (make(20_000), make(200_000));
    timing::grows_within(
        name,
        timing::TENFOLD,
        &mut || timing::once(|| run(&small, 20_000)),
        &mut || timing::once(|| run(&large, 200_000)),
    );
}

#[test]
fn many_distinct_claim_paths_are_linear() {
    linear(
        "claim_paths",
        |n| -> Vec<claims::Claim> {
            (0..n)
                .map(|i| claims::Claim {
                    kind: claims::Kind::Path,
                    rel_path: c(&format!("p{i}")),
                    name: vec![],
                    value: vec![],
                })
                .collect()
        },
        |cs, n| assert_eq!(claims::claim_paths(cs).len(), n),
    );
}

#[test]
fn many_distinct_patch_headers_are_linear() {
    linear(
        "patch_header_path",
        |n| -> Vec<Vec<u32>> { (0..n).map(|i| c(&format!("+++ b/f{i}"))).collect() },
        |lines, n| {
            let (mut f, mut d) = (claims::PathSet::new(), claims::PathSet::new());
            for line in lines {
                let _ = claims::patch_header_path(line, &[], &mut f, &mut d);
            }
            assert_eq!(f.len(), n);
        },
    );
}

#[test]
fn long_reasons_are_searched_in_linear_time() {
    linear(
        "claim_reason_is_indeterminate",
        |n| c(&"could not be rea".repeat(n)),
        |reason, _| assert!(!claims::claim_reason_is_indeterminate(reason)),
    );
}

#[test]
fn assign_annotation_without_equals_is_linear() {
    // `x:` then a long annotation that never meets `=`: a backtracking
    // engine retries every split of `[^=\n]+`.
    linear(
        "annotation",
        |n| {
            let mut line = c("x:");
            line.extend(std::iter::repeat_n(u32::from(b'a'), n));
            line.extend(c("=="));
            line
        },
        |line, _| assert!(claims::binding_token(line).is_none()),
    );
}

#[test]
fn long_identifiers_and_blanks_are_linear() {
    linear(
        "ident",
        |n| {
            let mut line = c("def ");
            line.extend(std::iter::repeat_n(u32::from(b'a'), n));
            line.extend(std::iter::repeat_n(0x20, n));
            line
        },
        |line, n| {
            assert!(claims::binding_token(line).is_none());
            assert!(claims::is_identifier(&line[4..4 + n]));
        },
    );
}

#[test]
fn long_hunk_headers_are_linear() {
    linear(
        "hunk",
        |n| c(&format!("@@ -{} +1 @@", "7".repeat(n))),
        |line, _| assert_eq!(claims::hunk_lines(line).unwrap().as_deref(), Some("2")),
    );
}

#[test]
fn long_header_paths_are_linear() {
    linear(
        "header",
        |n| {
            let p = "x".repeat(n);
            let rest = c(&format!("a/{p} b/{p}"));
            let mut line = c("+++ b/");
            line.extend(std::iter::repeat_n(0x3000, n));
            (rest, line)
        },
        |(rest, line), n| {
            assert_eq!(claims::header_path(rest).len(), n);
            let (mut f, mut d) = (claims::PathSet::new(), claims::PathSet::new());
            assert_eq!(
                claims::patch_header_path(line, &[], &mut f, &mut d),
                Some(vec![])
            );
        },
    );
}

#[test]
fn long_paths_spell_in_bounded_forms() {
    let mut path = Vec::new();
    for _ in 0..10_000 {
        path.extend(c("\u{212b}/"));
    }
    let t = Instant::now();
    assert!(claims::spellings_capped(&path));
    assert!(claims::stored_spellings(&path).len() <= 3);
    assert!(t.elapsed() < Duration::from_secs(5));
}

#[test]
fn digit_limit_edges() {
    for (n, ok) in [(4299, true), (4300, true), (4301, false), (100_000, false)] {
        let line = c(&format!("@@ -1 +1,{} @@", "9".repeat(n)));
        assert_eq!(claims::hunk_lines(&line).is_ok(), ok, "{n}");
    }
    let line = c(&format!(
        "@@ -1,{} +1,{} @@",
        "9".repeat(4300),
        "9".repeat(4300)
    ));
    let sum = claims::hunk_lines(&line).unwrap().unwrap();
    assert_eq!(sum.len(), 4301);
    assert!(sum.starts_with('1') && sum[1..sum.len() - 1].bytes().all(|b| b == b'9'));
    assert!(sum.ends_with('8'));
}

#[test]
fn every_code_point_panics_nowhere() {
    for cp in (0..0x11_0000u32).step_by(7) {
        let line = [u32::from(b'x'), cp, u32::from(b'='), cp];
        let _ = claims::binding_token(&line);
        let _ = claims::is_identifier(&line);
        let _ = claims::hunk_lines(&[0x40, 0x40, 0x20, 0x2D, cp, 0x20, 0x2B, cp, 0x20, 0x40, 0x40]);
        let _ = claims::stored_spellings(&[cp, 0x2F, cp]);
        let _ = claims::header_path(&[0x61, 0x2F, cp, 0x20, 0x62, 0x2F, cp]);
        let _ = claims::control_character(&line);
        let _ = claims::lone_surrogate(&line);
    }
    for t in [
        &[][..],
        &[0x0A][..],
        &[0x40, 0x40][..],
        &[0x2B, 0x2B, 0x2B][..],
    ] {
        let _ = claims::binding_token(t);
        let _ = claims::hunk_lines(t);
        let _ = claims::header_path(t);
        let (mut f, mut d) = (claims::PathSet::new(), claims::PathSet::new());
        let _ = claims::patch_header_path(t, t, &mut f, &mut d);
    }
}

// Unit 2: the claim parser and the literal readers.

#[test]
fn hostile_values_do_not_panic() {
    for v in [
        "[".repeat(100_000),
        "(".repeat(100_000) + "1",
        "-".repeat(100_000) + "1",
        "'".repeat(100_001),
        "1".repeat(100_000),
        "0x".to_string() + &"f".repeat(100_000),
        "{".repeat(50_000) + &"}".repeat(50_000),
    ] {
        let raw = c(&format!("p::N={v}"));
        let _ = claims::parse_claim(&raw);
        let _ = claims::rhs_repr(&c(&format!("N = {v}")));
        let _ = claims::string_fragment(&c(&v));
        let _ = claims::anchors_from_value(&c(&v));
        let lit = claims::Claim {
            kind: claims::Kind::Literal,
            rel_path: c("p"),
            name: c("N"),
            value: c(&v),
        };
        let _ = claims::claim_is_addressable(&lit);
    }
}

#[test]
fn trailing_commas_are_linear() {
    linear(
        "string_fragment",
        |n| c(&format!("'a'{}", ",".repeat(n))),
        |t, _| {
            assert_eq!(
                claims::string_fragment(t).unwrap().map(|s| s.len()),
                Some(1)
            )
        },
    );
}

#[test]
fn anchors_are_linear_in_lines() {
    linear(
        "anchors_from_value",
        |n| c(&format!("'{}'", "abcdefghijklmnop\\n".repeat(n / 10))),
        |t, _| assert_eq!(claims::anchors_from_value(t).unwrap().len(), 1),
    );
}

#[test]
fn governed_paths_are_linear_in_claims() {
    linear(
        "governed_claim_paths",
        |n| -> Vec<claims::Claim> {
            (0..n)
                .map(|i| claims::Claim {
                    kind: claims::Kind::Literal,
                    rel_path: c(&format!("p{}", i / 2)),
                    name: c("N"),
                    value: c(if i % 4 == 0 { "[1]" } else { "1" }),
                })
                .collect()
        },
        |cs, n| assert_eq!(claims::governed_claim_paths(cs).unwrap().len(), n / 4),
    );
}

#[test]
fn a_full_list_of_longest_claims_is_read() {
    let raw: Vec<Vec<u32>> = (0..claims::MAX_CLAIMS)
        .map(|i| c(&format!("p{i:04}::N='{}'", "x".repeat(1024 - 11))))
        .collect();
    assert!(raw.iter().all(|r| r.len() == 1024));
    let started = Instant::now();
    assert_eq!(
        claims::parse_claims(&raw).unwrap().len(),
        claims::MAX_CLAIMS
    );
    assert!(started.elapsed() < Duration::from_secs(20));
}
