//! Plausible wrong implementations of the checkout token's text, each
//! shown to answer differently from the reference on at least one row of
//! `checkout_token.jsonl` (and the port to answer as the reference on
//! those rows): the fixtures kill every one.

mod common;

use bettermemory_core::checkout_token::{self as ct, blake2b, Lineage, CROCKFORD};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use common::{jsonl, neutral, text_field, unhex};
use serde_json::Value;

fn rows(f: &str) -> Vec<Value> {
    jsonl("checkout_token.jsonl")
        .into_iter()
        .skip(1)
        .filter(|r| r["f"] == f)
        .collect()
}

fn out_text(row: &Value) -> Option<String> {
    let hex = row.get("r")?.get("hex")?.as_str()?;
    Some(String::from_utf8(unhex(hex)).expect("utf-8"))
}

fn lin(v: &Value) -> Lineage {
    let a = v.as_array().expect("a lineage");
    let s = |x: &Value| match neutral(x) {
        PyValue::Str(ref s) => s.clone(),
        _ => panic!("a str"),
    };
    Lineage {
        token: s(&a[0]),
        device: s(&a[1]),
        inode: s(&a[2]),
    }
}

/// Counts the rows of `f` on which `mutant` and the file disagree, after
/// checking the port agrees with the file on every one.
fn killed(
    f: &str,
    port: impl Fn(&Value) -> Option<String>,
    mutant: impl Fn(&Value) -> Option<String>,
) -> usize {
    let rows = rows(f);
    assert!(!rows.is_empty(), "no {f} rows");
    let mut differ = 0;
    for row in &rows {
        let want = out_text(row);
        assert_eq!(port(row), want, "port on {row}");
        if mutant(row) != want {
            differ += 1;
        }
    }
    differ
}

fn bits_lsb_first(n: usize, bytes: &[u8]) -> String {
    let bit = |i: usize| (bytes[i / 8] >> (i % 8)) & 1;
    (0..n)
        .map(|c| {
            let v = (0..5).fold(0usize, |v, j| (v << 1) | usize::from(bit(5 * c + j)));
            char::from(CROCKFORD[v])
        })
        .collect()
}

fn low_bits(n: usize, bytes: &[u8]) -> String {
    // The value without the shift: its low 5n bits.
    let total = bytes.len() * 8;
    let bit = |i: usize| (bytes[i / 8] >> (7 - i % 8)) & 1;
    let start = total - 5 * n;
    (0..n)
        .map(|c| {
            let v = (0..5).fold(0usize, |v, j| {
                (v << 1) | usize::from(bit(start + 5 * c + j))
            });
            char::from(CROCKFORD[v])
        })
        .collect()
}

fn rc(row: &Value, f: impl Fn(usize, &[u8]) -> String) -> Option<String> {
    let n = row["n"].as_i64()?;
    if n < 0 {
        return None;
    }
    Some(f(n as usize, &unhex(row["b"].as_str()?)))
}

fn rc_port(row: &Value) -> Option<String> {
    ct::random_crockford(row["n"].as_i64()?, &unhex(row["b"].as_str()?)).ok()
}

#[test]
fn random_text_mutants_are_killed() {
    assert!(killed("rc", rc_port, |r| rc(r, bits_lsb_first)) > 0);
    assert!(killed("rc", rc_port, |r| rc(r, low_bits)) > 0);
}

fn link_with(l: &Lineage, nonce: &[u32], prefixed: bool, person: &[u8], high: bool) -> String {
    let enc = |s: &PyStr| -> Vec<u8> {
        s.iter()
            .map(|&c| char::from_u32(c).unwrap_or('\u{fffd}'))
            .collect::<String>()
            .into_bytes()
    };
    let mut text = Vec::new();
    for f in [&l.token, &l.device, &l.inode, &nonce.to_vec()] {
        let b = enc(f);
        if prefixed {
            text.extend_from_slice(format!("{}:", b.len()).as_bytes());
        }
        text.extend_from_slice(&b);
        text.push(b',');
    }
    let d = blake2b::blake2b(&text, 16, person).expect("digest");
    let v = d.iter().fold(0u128, |v, &b| (v << 8) | u128::from(b));
    (0..13)
        .rev()
        .map(|i| {
            let shift = if high { 128 - 5 * (13 - i) } else { 5 * i };
            char::from(CROCKFORD[((v >> shift) & 31) as usize])
        })
        .collect()
}

#[test]
fn link_mutants_are_killed() {
    let port = |r: &Value| ct::link(&lin(&r["l"]), &text_field(r, "n")).ok();
    let m = |prefixed, person: &'static [u8], high| {
        move |r: &Value| {
            let l = lin(&r["l"]);
            let n = text_field(r, "n");
            if l.device
                .iter()
                .chain(&l.inode)
                .chain(&n)
                .any(|&c| (0xd800..0xe000).contains(&c))
            {
                return None;
            }
            Some(link_with(&l, &n, prefixed, person, high))
        }
    };
    assert_eq!(killed("link", port, m(true, ct::LINK_PERSON, false)), 0);
    assert!(killed("link", port, m(false, ct::LINK_PERSON, false)) > 0);
    assert!(killed("link", port, m(true, b"", false)) > 0);
    assert!(killed("link", port, m(true, ct::LINK_PERSON, true)) > 0);
}

fn bool_rows(f: &str, port: impl Fn(&Value) -> bool, mutant: impl Fn(&Value) -> bool) -> usize {
    let rows = rows(f);
    assert!(!rows.is_empty());
    let mut differ = 0;
    for row in &rows {
        let want = row["r"].as_bool().expect("a bool");
        assert_eq!(port(row), want, "port on {row}");
        if mutant(row) != want {
            differ += 1;
        }
    }
    differ
}

#[test]
fn valid_mutants_are_killed() {
    let port = |r: &Value| ct::valid(&neutral(&r["a"]));
    let lower = |r: &Value| match neutral(&r["a"]) {
        PyValue::Str(ref s) => {
            let up: PyStr = s
                .iter()
                .map(|&c| {
                    if (0x61..=0x7a).contains(&c) {
                        c - 32
                    } else {
                        c
                    }
                })
                .collect();
            ct::token_text(&up)
        }
        _ => false,
    };
    let any_alnum = |r: &Value| match neutral(&r["a"]) {
        PyValue::Str(ref s) => {
            s.len() == 26
                && s.iter()
                    .all(|&c| char::from_u32(c).is_some_and(|c| c.is_ascii_alphanumeric()))
        }
        _ => false,
    };
    let unicode_alnum = |r: &Value| match neutral(&r["a"]) {
        PyValue::Str(ref s) => {
            s.len() == 26
                && s.iter().all(|&c| {
                    char::from_u32(c).is_some_and(|ch| {
                        ct::token_text(&[c; 26]) || (ch.is_alphanumeric() && !ch.is_ascii())
                    })
                })
        }
        _ => false,
    };
    assert!(bool_rows("valid", port, lower) > 0);
    assert!(bool_rows("valid", port, any_alnum) > 0);
    assert!(bool_rows("valid", port, unicode_alnum) > 0);
}

fn decimal_loose(s: &[u32]) -> bool {
    let t: String = s.iter().filter_map(|&c| char::from_u32(c)).collect();
    t.trim().parse::<i128>().is_ok()
}

#[test]
fn lineage_mutants_are_killed() {
    let rows = rows("lineage");
    let mut differ = 0;
    for row in &rows {
        let a = row["a"].as_array().expect("a");
        let (token, key) = (neutral(&a[0]), neutral(&a[1]));
        let want = !row["r"].is_null();
        assert_eq!(ct::lineage(&token, &key).is_some(), want, "{row}");
        let loose = ct::valid(&token)
            && match &key {
                PyValue::List(k) | PyValue::Tuple(k) if k.len() >= 2 => k[..2]
                    .iter()
                    .all(|x| matches!(x, PyValue::Str(s) if decimal_loose(s))),
                _ => false,
            };
        if loose != want {
            differ += 1;
        }
    }
    assert!(differ > 0, "a trimmed integer parse is not told apart");
}

fn parse_mutant(text: &[u32], strip_all_cr: bool, keep_empty: bool) -> Option<PyStr> {
    let mut lines: Vec<&[u32]> = text.split(|&c| c == 10).collect();
    if !keep_empty && lines.last().is_some_and(|l| l.is_empty()) {
        lines.pop();
    }
    if lines.len() != 2 {
        return None;
    }
    let strip = |l: &[u32]| -> PyStr {
        if strip_all_cr {
            let mut v = l.to_vec();
            while v.last() == Some(&13) {
                v.pop();
            }
            v
        } else {
            l.to_vec()
        }
    };
    let header: PyStr = ct::TOKEN_HEADER.chars().map(u32::from).collect();
    let value = strip(lines[1]);
    (strip(lines[0]) == header && ct::token_text(&value)).then_some(value)
}

#[test]
fn parse_mutants_are_killed() {
    let rows = rows("parse");
    let (mut no_cr, mut all_cr, mut empty) = (0, 0, 0);
    for row in &rows {
        let a = text_field(row, "a");
        let want = !row["r"].is_null();
        assert_eq!(ct::parse(&a).is_some(), want, "{row}");
        no_cr += usize::from(parse_mutant(&a, false, false).is_some() != want);
        all_cr += usize::from(parse_mutant(&a, true, false).is_some() != want);
        empty += usize::from(parse_mutant(&a, false, true).is_some() != want);
    }
    assert!(
        no_cr > 0 && all_cr > 0 && empty > 0,
        "{no_cr} {all_cr} {empty}"
    );
}

#[test]
fn cut_short_mutants_are_killed() {
    let rows = rows("detail");
    let (mut le, mut empty_false) = (0, 0);
    for row in &rows {
        let a = text_field(row, "a");
        let want = row["cs"].as_bool().expect("cs");
        assert_eq!(ct::cut_short(&a), want, "{row}");
        let head: PyStr = format!("{}\n", ct::TOKEN_HEADER)
            .chars()
            .map(u32::from)
            .collect();
        let at_most = if a.len() <= head.len() {
            head.starts_with(&a)
        } else {
            a.starts_with(&head)
                && a.len() - head.len() <= 26
                && a[head.len()..]
                    .iter()
                    .all(|&c| c < 128 && CROCKFORD.contains(&(c as u8)))
        };
        le += usize::from(at_most != want);
        empty_false += usize::from((!a.is_empty() && ct::cut_short(&a)) != want);
    }
    assert!(le > 0 && empty_false > 0, "{le} {empty_false}");
}

#[test]
fn token_path_mutant_is_killed() {
    let rows = rows("path");
    let mut differ = 0;
    for row in &rows {
        let a = row["a"].as_str().expect("a");
        let want = row["r"].as_str().expect("r");
        let naive = format!("{a}/info/bettermemory");
        differ += usize::from(naive != want);
    }
    assert!(differ > 0);
}

#[test]
fn render_mutant_is_killed() {
    let rows = rows("render");
    let mut differ = 0;
    for row in &rows {
        let a = text_field(row, "a");
        let raised = row.get("x").is_some();
        assert_eq!(ct::render(&a).is_err(), raised, "{row}");
        // UTF-8 instead of ASCII: raises only on a surrogate.
        let utf8_raises = a.iter().any(|&c| (0xd800..0xe000).contains(&c));
        differ += usize::from(utf8_raises != raised);
    }
    assert!(differ > 0);
}

#[test]
fn blake2b_takes_a_sixteen_byte_person() {
    // hashlib.blake2b(b"", digest_size=16, person=bytes(16)) and
    // hashlib.blake2b(b"abc", digest_size=16, person=b"0123456789abcdef").
    let zero = blake2b::blake2b(b"", 16, &[0u8; 16]).expect("16 bytes");
    assert_eq!(unhex("cae66941d9efbd404e4d88758ea67670"), zero);
    let abc = blake2b::blake2b(b"abc", 16, b"0123456789abcdef").expect("16 bytes");
    assert_eq!(unhex("4243ca07c67e8dc379d9036ae7386a2f"), abc);
    assert!(blake2b::blake2b(b"", 16, &[0u8; 17]).is_none());
}
