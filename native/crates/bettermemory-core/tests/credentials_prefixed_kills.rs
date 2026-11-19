//! Inputs a plausible wrong implementation of `credentials::prefixed`
//! answers otherwise: an ASCII-only `\b`, a mask threshold off by one, a
//! greedy body that never gives back, a missing spent run, a PEM `\r`
//! read as a line end, a JWT that restarts inside its own token. Each expectation is the reference's
//! answer (`_PREFIXED_DETECTORS` through `finditer`, spent runs and masks
//! left out), read from CPython 3.13.13.

use bettermemory_core::credentials::prefixed::{prefixed_spans, Detector};

type Span = (usize, usize, &'static str);

const CASES: &[(&str, &[Span], &str)] = &[
    (
        concat!("\u{e9}AKI", "AIOS", "FODN", "N7EX", "AMPL", "0"),
        &[],
        "a Latin-1 letter is a word character before the prefix",
    ),
    (
        concat!("AKIA", "IOSF", "ODNN", "7EXA", "MPL0", "\u{e9}"),
        &[],
        "a Latin-1 letter is a word character after the body",
    ),
    ("AKIAABCDABCDABCDABCD", &[], "masked at four distinct"),
    (
        concat!("AKIA", "ABCD", "EABC", "DEAB", "CDEA"),
        &[(0, 20, "aws-access-key-id")],
        "not masked at five distinct",
    ),
    (
        concat!("sk-A", "BCDE", "FGHI", "J012", "3456", "789-", "\u{e9}"),
        &[(0, 24, "openai-anthropic-key")],
        "the boundary after the hyphen",
    ),
    (
        concat!("sk-a", "nt-a", "aaaa", "aaaa", "aaaa", "aaaa", "aaaA", "1"),
        &[(0, 29, "openai-anthropic-key")],
        "an ant- body with its digit at the end",
    ),
    (
        concat!("sk-a", "nt-A", "1bbb", "bbbb", "bbbb", "bbb"),
        &[(0, 23, "openai-anthropic-key")],
        "an ant- body one short, the bare body long enough",
    ),
    (
        concat!("sk-s", "k-AB", "CDEF", "GHIJ", "0123", "4567", "89"),
        &[(0, 26, "openai-anthropic-key")],
        "the body runs through the later prefix",
    ),
    (
        "sk-image-processing-toolkit-v3",
        &[],
        "lookaheads on the body",
    ),
    (
        concat!("xoxb", "-123", "4567", "890-", "\u{e9}"),
        &[(0, 16, "slack-token")],
        "slack ends past the hyphen",
    ),
    (
        concat!("xoxb", "-you", "r-bo", "t-to", "ken ", "xoxb", "-123", "4567", "890"),
        &[(20, 35, "slack-token")],
        "slack digit lookahead",
    ),
    (
        concat!("gith", "ub_p", "at_a", "bcde", "abcd", "eabc", "deab", "cdea", "b_"),
        &[(0, 34, "github-token")],
        "pat class holds the underscore",
    ),
    (
        "-----BEGIN PRIVATE KEY-----\rAAAAAAAAAAAAAAAA",
        &[],
        "a lone carriage return",
    ),
    (
        "-----BEGIN RSA  PRIVATE KEY-----\nAAAAAAAAAAAAAAAA",
        &[],
        "one label at most",
    ),
    (
        "-----BEGIN PRIVATE KEY----- \t\r\nDEK-Info:",
        &[(0, 27, "private-key-pem")],
        "spaces then CRLF then a header",
    ),
    (
        concat!("eyJa", "aaaa", "aaa.", "bbbb", "bbbb", ".ccc", "cccc", "-d\u{e9}"),
        &[(0, 29, "jwt")],
        "jwt gives back in the signature",
    ),
    (
        concat!("eyJa", "aaaa", "aaa.", "eyJb", "bbbb", "bbb.", "cccc", "cccc"),
        &[(0, 32, "jwt")],
        "jwt inner eyJ is not a new start",
    ),
    (
        "hvs.aaaaaaaaaaaaaaaaaaaaaaa-",
        &[],
        "vault minimum counts the hyphen",
    ),
    (
        "sk_live_aaaaaaaaaaaaaaaaaaaaaaaa_",
        &[],
        "stripe needs a boundary after",
    ),
    (
        "AIzaBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB-",
        &[],
        "google fixed length then hyphen",
    ),
    (
        "ghp_aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5x",
        &[],
        "github fixed length",
    ),
    (
        "AKIA\u{661}OSFODNN7EXAMPL0",
        &[],
        "the body class is ASCII digits",
    ),
];

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

#[test]
fn each_kill_answers_as_the_reference() {
    for (text, want, why) in CASES {
        assert_eq!(prefixed_spans(&cps(text)), want.to_vec(), "{why}: {text:?}");
    }
}

#[test]
fn a_surrogate_is_not_a_word_character() {
    let mut text = vec![0xD800];
    text.extend(cps(concat!("AKIA", "IOSF", "ODNN", "7EXA", "MPL0")));
    assert_eq!(prefixed_spans(&text), vec![(1, 21, "aws-access-key-id")]);
}

#[test]
fn a_spent_run_is_a_match_and_no_hit() {
    let text = cps("sk-sk-sk-");
    let found = Detector::OpenAiAnthropic.find_iter(&text);
    assert_eq!(found.len(), 1);
    assert!(found[0].spent);
    assert_eq!((found[0].start, found[0].end), (0, 9));
    let text = cps("eyJ");
    let found = Detector::Jwt.find_iter(&text);
    assert_eq!((found[0].start, found[0].end, found[0].spent), (0, 3, true));
}
