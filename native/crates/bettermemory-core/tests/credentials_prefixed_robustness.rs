//! Hostile input for `credentials::prefixed`: runs of nothing but prefixes
//! (the shapes the reference's spent runs exist for), a body that gives
//! back across 300,000 hyphens, repeated headers and tokens, the integer
//! edges of the text (empty, a prefix cut at the end). Nothing panics, the
//! answers are the reference's, and each input of about 300 KB takes well
//! under the bound: the reference takes 14 to 24 ms for each on the
//! machine the README names, and the release build is held to 10 times
//! that (0.25 s), the debug build to 8 s.

use std::time::{Duration, Instant};

use bettermemory_core::credentials::prefixed::{
    is_masked_token, prefixed_spans, Detector, Match, DETECTORS,
};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn bound() -> Duration {
    if cfg!(debug_assertions) {
        Duration::from_secs(8)
    } else {
        Duration::from_millis(250)
    }
}

/// The fastest of three runs is held to the bound, so a runner that is
/// busy for one run does not fail the test; a scan that is not linear is
/// slow on every run.
fn timed(name: &str, text: &[u32]) -> Vec<(usize, usize, &'static str)> {
    let mut best = Duration::MAX;
    let mut out = Vec::new();
    for _ in 0..3 {
        let started = Instant::now();
        out = prefixed_spans(text);
        best = best.min(started.elapsed());
        if best < bound() {
            break;
        }
    }
    assert!(best < bound(), "{name}: {best:?}");
    out
}

fn rep(piece: &str, n: usize) -> Vec<u32> {
    cps(&piece.repeat(n))
}

#[test]
fn prefix_runs_take_linear_time() {
    for (name, piece, n) in [
        ("sk", "sk-", 100_000),
        ("xoxb", "xoxb-", 60_000),
        ("xapp", "xapp-", 60_000),
        ("eyJ", "eyJ", 100_000),
        ("pem", "-----BEGIN PRIVATE KEY-----", 10_000),
        ("hyphens", "-", 300_000),
        ("letters", "a", 300_000),
    ] {
        assert!(timed(name, &rep(piece, n)).is_empty(), "{name}");
    }
    // The first fine-grained prefix's body runs to the end of the text.
    let t = rep("github_pat_", 30_000);
    assert_eq!(timed("pat", &t), vec![(0, 330_000, "github-token")]);
}

#[test]
fn a_body_gives_back_across_a_long_run() {
    let mut t = cps("sk-A1");
    t.extend(rep("-", 300_000));
    t.push(0xE9);
    assert_eq!(
        timed("giveback", &t),
        vec![(0, 300_005, "openai-anthropic-key")]
    );
    let mut t = cps("xoxb-1");
    t.extend(rep("-", 300_000));
    t.push(0xE9);
    assert_eq!(timed("slack", &t), vec![(0, 300_006, "slack-token")]);
    let mut t = cps("hvs.");
    t.extend(rep("a-", 150_000));
    t.push(0xE9);
    assert_eq!(timed("vault", &t), vec![(0, 300_004, "vault-token")]);
}

#[test]
fn many_tokens_are_each_found() {
    let t = rep("eyJaaaaaaaa.", 25_000);
    assert_eq!(timed("jwt", &t).len(), 8_333);
    let t = rep(concat!(" AKI", "AIOS", "FODN", "N7EX", "AMPL", "0"), 15_000);
    assert_eq!(timed("aws", &t).len(), 15_000);
}

#[test]
fn the_edges_of_a_text() {
    assert!(prefixed_spans(&[]).is_empty());
    for cut in [
        "s",
        "sk",
        "sk-",
        "AKIA",
        "xox",
        "xoxb",
        "xapp",
        "eyJ",
        "SG.",
        "hvs",
        "hvs.",
        "-----BEGIN ",
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----\r",
        "github_pat",
        "ghp_",
        "AIza",
        "sk_live",
        "sk_live_",
    ] {
        let t = cps(cut);
        for d in DETECTORS {
            for m in d.find_iter(&t) {
                assert!(m.start <= m.end && m.end <= t.len(), "{cut}");
            }
        }
        assert!(prefixed_spans(&t).is_empty(), "{cut}");
    }
    let found = Detector::OpenAiAnthropic.find_iter(&[u32::from(b's'), u32::from(b'k'), 0x2D]);
    assert_eq!(found.len(), 1);
    assert!(found[0].spent);
    let odd = [0x10FFFF, 0xDFFF, 0, u32::MAX, 0x110000];
    assert!(prefixed_spans(&odd).is_empty());
}

#[test]
fn a_hand_built_match_outside_the_text() {
    let text = [0u32; 10];
    for body in [(5, 3), (0, 11), (11, 12), (usize::MAX, 0)] {
        let m = Match {
            start: 0,
            end: 0,
            body: Some(body),
            spent: false,
        };
        assert!(!is_masked_token(&text, &m), "{body:?}");
    }
    let m = Match {
        start: 0,
        end: 0,
        body: Some((0, 10)),
        spent: false,
    };
    assert!(is_masked_token(&text, &m));
}
