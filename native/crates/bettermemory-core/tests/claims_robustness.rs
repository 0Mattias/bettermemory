//! `claims`' leaves over hostile input: very long lines, long runs that a
//! backtracking reading of the patterns would retry, every code point, and
//! the digit limit's edge. No panic, and time linear in the input: ten
//! times the input costs under thirty times the time.

use std::time::{Duration, Instant};

use bettermemory_core::claims;

fn c(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn timed(f: impl Fn(usize)) -> (Duration, Duration) {
    f(1_000);
    let t = Instant::now();
    f(20_000);
    let small = t.elapsed();
    let t = Instant::now();
    f(200_000);
    (small, t.elapsed())
}

/// Compares the fastest run seen of each size over up to five rounds, so
/// time stolen by other work on the machine, which only ever adds, drops
/// out; a quadratic path stays a hundred times slower in every round.
fn linear(name: &str, f: impl Fn(usize)) {
    let mut best: Option<(Duration, Duration)> = None;
    for _ in 0..5 {
        let (small, big) = timed(&f);
        let (s, b) = best.map_or((small, big), |(s, b)| (s.min(small), b.min(big)));
        best = Some((s, b));
        if b <= s * 30 + Duration::from_millis(50) {
            return;
        }
    }
    panic!("{name}: fastest {best:?}");
}

#[test]
fn many_distinct_claim_paths_are_linear() {
    linear("claim_paths", |n| {
        let cs: Vec<claims::Claim> = (0..n)
            .map(|i| claims::Claim {
                kind: claims::Kind::Path,
                rel_path: c(&format!("p{i}")),
                name: vec![],
                value: vec![],
            })
            .collect();
        assert_eq!(claims::claim_paths(&cs).len(), n);
    });
}

#[test]
fn many_distinct_patch_headers_are_linear() {
    linear("patch_header_path", |n| {
        let (mut f, mut d) = (claims::PathSet::new(), claims::PathSet::new());
        for i in 0..n {
            let line = c(&format!("+++ b/f{i}"));
            let _ = claims::patch_header_path(&line, &[], &mut f, &mut d);
        }
        assert_eq!(f.len(), n);
    });
}

#[test]
fn long_reasons_are_searched_in_linear_time() {
    linear("claim_reason_is_indeterminate", |n| {
        let reason = c(&"could not be rea".repeat(n));
        assert!(!claims::claim_reason_is_indeterminate(&reason));
    });
}

#[test]
fn assign_annotation_without_equals_is_linear() {
    // `x:` then a long annotation that never meets `=`: a backtracking
    // engine retries every split of `[^=\n]+`.
    linear("annotation", |n| {
        let mut line = c("x:");
        line.extend(std::iter::repeat_n(u32::from(b'a'), n));
        line.extend(c("=="));
        assert!(claims::binding_token(&line).is_none());
    });
}

#[test]
fn long_identifiers_and_blanks_are_linear() {
    linear("ident", |n| {
        let mut line = c("def ");
        line.extend(std::iter::repeat_n(u32::from(b'a'), n));
        line.extend(std::iter::repeat_n(0x20, n));
        assert!(claims::binding_token(&line).is_none());
        assert!(claims::is_identifier(&line[4..4 + n]));
    });
}

#[test]
fn long_hunk_headers_are_linear() {
    linear("hunk", |n| {
        let line = c(&format!("@@ -{} +1 @@", "7".repeat(n)));
        assert_eq!(claims::hunk_lines(&line).unwrap().as_deref(), Some("2"));
    });
}

#[test]
fn long_header_paths_are_linear() {
    linear("header", |n| {
        let p = "x".repeat(n);
        let rest = c(&format!("a/{p} b/{p}"));
        assert_eq!(claims::header_path(&rest).len(), n);
        let mut line = c("+++ b/");
        line.extend(std::iter::repeat_n(0x3000, n));
        let (mut f, mut d) = (claims::PathSet::new(), claims::PathSet::new());
        assert_eq!(
            claims::patch_header_path(&line, &[], &mut f, &mut d),
            Some(vec![])
        );
    });
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
