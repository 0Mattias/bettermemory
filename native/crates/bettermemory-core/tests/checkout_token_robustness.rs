//! The checkout token's text over hostile input: very large texts and
//! fields, deeply nested values, integer bounds and every byte count
//! around the one asked for. Nothing panics, and each function is linear:
//! eight times the input costs under thirty-two times the time.

use std::time::{Duration, Instant};

use bettermemory_core::checkout_token::{self as ct, blake2b, Lineage, Raised};
use bettermemory_core::pyvalue::{PyStr, PyValue};

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn timed(f: impl FnOnce()) -> Duration {
    let t = Instant::now();
    f();
    t.elapsed()
}

fn linear(name: &str, run: impl Fn(usize)) {
    run(1_000);
    // The fastest of three runs each, so a loaded machine does not decide.
    let best = |n: usize| (0..3).map(|_| timed(|| run(n))).min().unwrap_or_default();
    let small = best(200_000);
    let large = best(1_600_000);
    assert!(
        large <= small * 32 + Duration::from_millis(250),
        "{name}: {small:?} then {large:?}"
    );
}

#[test]
fn integer_bounds_and_byte_counts() {
    for n in [i64::MIN, -1] {
        assert_eq!(ct::random_bytes_needed(n), None);
        assert_eq!(ct::random_crockford(n, &[]), Err(Raised::NegativeBits));
    }
    assert_eq!(ct::random_bytes_needed(0), Some(0));
    assert_eq!(ct::random_crockford(0, &[]), Ok(String::new()));
    assert!(ct::random_bytes_needed(i64::MAX).is_some());
    assert!(matches!(
        ct::random_crockford(i64::MAX, &[0; 3]),
        Err(Raised::RandomBytes { .. })
    ));
    for n in 0..64i64 {
        let need = ct::random_bytes_needed(n).unwrap();
        for given in [need.saturating_sub(1), need + 1] {
            if given != need {
                assert!(ct::random_crockford(n, &vec![7; given]).is_err());
            }
        }
        let text = ct::random_crockford(n, &vec![0xa5; need]).unwrap();
        assert_eq!(text.len(), n as usize);
    }
    assert!(ct::mint(&[0; 16]).is_err());
    assert_eq!(ct::mint(&[0; 17]).unwrap(), "0".repeat(26));
    assert_eq!(ct::mint(&[0xff; 17]).unwrap(), "Z".repeat(26));
}

#[test]
fn blake2b_bounds() {
    assert!(blake2b::blake2b(b"", 0, b"").is_none());
    assert!(blake2b::blake2b(b"", 65, b"").is_none());
    assert!(blake2b::blake2b(b"", 1, &[0; 17]).is_none());
    // RFC 7693 appendix A: BLAKE2b-512 of "abc".
    let d = blake2b::blake2b(b"abc", 64, b"").unwrap();
    assert_eq!(&d[..4], &[0xba, 0x80, 0xa5, 0x3f]);
    for len in [127usize, 128, 129, 255, 256, 257] {
        assert_eq!(
            blake2b::blake2b(&vec![1; len], 16, ct::LINK_PERSON)
                .unwrap()
                .len(),
            16
        );
    }
}

fn nest(depth: usize) -> PyValue {
    let mut v = PyValue::str("1");
    for i in 0..depth {
        v = if i % 2 == 0 {
            PyValue::List(vec![v, PyValue::str("2")])
        } else {
            PyValue::Tuple(vec![v, PyValue::str("2"), PyValue::str("3")])
        };
    }
    v
}

#[test]
fn deep_values_answer_none() {
    let deep = nest(100_000);
    assert!(!ct::valid(&deep));
    assert!(Lineage::read(&deep).is_none());
    let token = PyValue::str("0123456789ABCDEFGHJKMNPQRS");
    assert!(ct::lineage(&token, &deep).is_none());
    assert_eq!(ct::succeeds(&deep, None), Ok(false));
}

#[test]
fn large_inputs_are_linear() {
    let header = format!("{}\n", ct::TOKEN_HEADER);
    linear("parse", |n| {
        let mut text = cps(&header);
        text.extend(std::iter::repeat_n(u32::from(b'7'), n));
        assert!(ct::parse(&text).is_none());
        let lines: PyStr = std::iter::repeat_n(10u32, n).collect();
        assert!(ct::parse(&lines).is_none());
    });
    linear("cut_short", |n| {
        let mut text = cps(&header);
        text.extend(std::iter::repeat_n(u32::from(b'7'), n));
        assert!(!ct::cut_short(&text));
        assert_eq!(
            ct::malformed_detail(&text),
            format!("not a {} file", ct::TOKEN_HEADER)
        );
    });
    linear("valid", |n| {
        let s = PyValue::Str(std::iter::repeat_n(u32::from(b'0'), n).collect());
        assert!(!ct::valid(&s));
    });
    linear("lineage", |n| {
        let token = PyValue::str("0123456789ABCDEFGHJKMNPQRS");
        let digits = PyValue::Str(std::iter::repeat_n(u32::from(b'9'), n).collect());
        let key = PyValue::Tuple(vec![digits.clone(), digits]);
        assert!(ct::lineage(&token, &key).is_some());
    });
    linear("link", |n| {
        let l = Lineage {
            token: cps("0123456789ABCDEFGHJKMNPQRS"),
            device: std::iter::repeat_n(0x1f600u32, n).collect(),
            inode: cps("1"),
        };
        assert_eq!(ct::link(&l, &cps("0000000000000")).unwrap().len(), 13);
    });
    linear("render", |n| {
        let t: PyStr = std::iter::repeat_n(u32::from(b'A'), n).collect();
        assert_eq!(
            ct::render(&t).unwrap().len(),
            n + ct::TOKEN_HEADER.len() + 2
        );
    });
    linear("random_crockford", |n| {
        let need = ct::random_bytes_needed(n as i64).unwrap();
        assert_eq!(
            ct::random_crockford(n as i64, &vec![0x5a; need])
                .unwrap()
                .len(),
            n
        );
    });
    linear("token_path", |n| {
        let path = "/a".repeat(n / 2);
        assert!(
            ct::token_path(&path, bettermemory_core::fsdata::Flavour::Posix)
                .ends_with("/info/bettermemory")
        );
    });
}

#[test]
fn surrogates_raise_and_never_panic() {
    let l = Lineage {
        token: cps("0123456789ABCDEFGHJKMNPQRS"),
        device: vec![0xd800],
        inode: vec![0xdfff, 0x10ffff],
    };
    assert_eq!(ct::link(&l, &cps("0")), Err(Raised::Encode));
    assert_eq!(ct::successor(&l, &[0; 9]), Err(Raised::Encode));
    assert_eq!(ct::render(&[0xd800]), Err(Raised::Encode));
    assert!(ct::parse(&[0xd800, 10, 0xdc00]).is_none());
    assert!(!ct::cut_short(&[0xd800]));
    for c in [
        0u32,
        0x7f,
        0x80,
        0xd7ff,
        0xe000,
        0x10ffff,
        0x110000,
        u32::MAX,
    ] {
        let mut t = cps("0123456789ABCDEFGHJKMNPQR");
        t.push(c);
        assert!(!ct::valid(&PyValue::Str(t.clone())));
        let _ = ct::render(&t);
        let _ = ct::parse(&t);
        let _ = ct::malformed_detail(&t);
    }
}
