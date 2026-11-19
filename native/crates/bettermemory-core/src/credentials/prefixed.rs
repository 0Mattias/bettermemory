//! `_PREFIXED_DETECTORS` (`credentials.py` 85-175) with `_is_masked_token`
//! and `_is_spent_run` (184-197): the twelve vendor-prefixed token shapes,
//! each matched as `re.finditer` matches its pattern, by a hand-written
//! matcher per pattern.
//!
//! Texts are code points ([`PyStr`]) and every offset is a code point
//! index, as a `str` match's `start()` and `end()` are. Each pattern reads
//! ASCII classes only, except `\b`, which is `re`'s Unicode word class
//! ([`unicode::is_word`], Unicode 15.1.0). No pattern can match the empty
//! string, so the scan resumes at a match's end and otherwise one code
//! point on, as `finditer` does.

use crate::pyvalue::PyStr;
use crate::unicode;

/// `_MIN_BODY_DIVERSITY`: a body with fewer distinct characters is a mask.
pub const MIN_BODY_DIVERSITY: usize = 5;

/// One entry of `_PREFIXED_DETECTORS`, in the reference's order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Detector {
    /// `\b(?:AKIA|ASIA)(?P<body>[0-9A-Z]{16})\b`
    AwsAccessKeyId,
    /// `\bsk-(?:(?:ant-|proj-)?(?=C*[A-Z])(?=C*[0-9])C{20,}\b|(?P<spent>C*))`
    OpenAiAnthropic,
    /// `\b[sr]k_(?:live|test)_[0-9A-Za-z]{24,}\b`
    Stripe,
    /// `\b(?:ghp|gho|ghu|ghs|ghr)_(?P<body>[A-Za-z0-9]{36})\b`
    GithubToken,
    /// `\bgithub_pat_(?P<body>[0-9A-Za-z_]{22,})\b`
    GithubPat,
    /// `\bxox[abceprs]-(?:(?=D*[0-9])D{10,}\b|(?P<spent>D*))`
    Slack,
    /// `\bxapp-(?:(?=D*[0-9])D{10,}\b|(?P<spent>D*))`
    SlackApp,
    /// `\bAIza[0-9A-Za-z_-]{35}\b`
    GoogleApiKey,
    /// `\bSG\.C{22}\.C{43}\b`
    SendGrid,
    /// `\bhv[sbr]\.C{24,}\b`
    Vault,
    /// The PEM private-key header with its key-material lookahead.
    PrivateKeyPem,
    /// `\beyJ(?:C{8,}\.C{8,}\.C{8,}\b|(?P<spent>C*))`
    Jwt,
}

/// `_PREFIXED_DETECTORS`' order.
pub const DETECTORS: [Detector; 12] = [
    Detector::AwsAccessKeyId,
    Detector::OpenAiAnthropic,
    Detector::Stripe,
    Detector::GithubToken,
    Detector::GithubPat,
    Detector::Slack,
    Detector::SlackApp,
    Detector::GoogleApiKey,
    Detector::SendGrid,
    Detector::Vault,
    Detector::PrivateKeyPem,
    Detector::Jwt,
];

/// One match of a detector's pattern, as `finditer` yields it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Match {
    pub start: usize,
    pub end: usize,
    /// The `body` group's span, for the patterns that name one.
    pub body: Option<(usize, usize)>,
    /// The `spent` group took part (it may be empty).
    pub spent: bool,
}

/// `[A-Za-z0-9_-]`, the class of the `sk-`, Google, SendGrid, Vault and
/// JWT bodies.
fn c_class(cp: u32) -> bool {
    cp < 0x80 && {
        let b = cp as u8;
        b.is_ascii_alphanumeric() || b == b'_' || b == b'-'
    }
}

/// `[0-9A-Za-z-]`, the Slack bodies.
fn d_class(cp: u32) -> bool {
    cp < 0x80 && {
        let b = cp as u8;
        b.is_ascii_alphanumeric() || b == b'-'
    }
}

fn alnum(cp: u32) -> bool {
    cp < 0x80 && (cp as u8).is_ascii_alphanumeric()
}

fn alnum_under(cp: u32) -> bool {
    alnum(cp) || cp == u32::from(b'_')
}

fn digit_upper(cp: u32) -> bool {
    cp < 0x80 && {
        let b = cp as u8;
        b.is_ascii_digit() || b.is_ascii_uppercase()
    }
}

fn pem_body(cp: u32) -> bool {
    c_class(cp) || cp == u32::from(b'+') || cp == u32::from(b'/') || cp == u32::from(b'=')
}

fn word_at(text: &[u32], i: usize) -> bool {
    i < text.len() && unicode::is_word(text[i])
}

/// `\b` at `i`.
fn boundary(text: &[u32], i: usize) -> bool {
    let before = i > 0 && word_at(text, i - 1);
    before != word_at(text, i)
}

fn lit(text: &[u32], i: usize, s: &str) -> bool {
    let b = s.as_bytes();
    i + b.len() <= text.len()
        && text[i..i + b.len()]
            .iter()
            .zip(b)
            .all(|(&c, &x)| c == u32::from(x))
}

/// The end of the run of `class` from `i`.
fn run(text: &[u32], i: usize, class: fn(u32) -> bool) -> usize {
    let mut e = i;
    while e < text.len() && class(text[e]) {
        e += 1;
    }
    e
}

/// `class{n}` from `i`: the end when the next `n` all belong.
fn exactly(text: &[u32], i: usize, n: usize, class: fn(u32) -> bool) -> Option<usize> {
    (i + n <= text.len() && text[i..i + n].iter().all(|&c| class(c))).then_some(i + n)
}

/// `class{min,}\b` from `s` over a run ending at `e`: greedy, giving back
/// one at a time until `\b` holds.
fn greedy_to_boundary(text: &[u32], s: usize, e: usize, min: usize) -> Option<usize> {
    if e < s + min {
        return None;
    }
    (s + min..=e).rev().find(|&k| boundary(text, k))
}

fn any_in(text: &[u32], s: usize, e: usize, pred: fn(u32) -> bool) -> bool {
    text[s..e].iter().any(|&c| pred(c))
}

fn upper(cp: u32) -> bool {
    cp < 0x80 && (cp as u8).is_ascii_uppercase()
}

fn digit(cp: u32) -> bool {
    cp < 0x80 && (cp as u8).is_ascii_digit()
}

fn hit(start: usize, end: usize, body: Option<(usize, usize)>) -> Option<Match> {
    Some(Match {
        start,
        end,
        body,
        spent: false,
    })
}

fn spent(start: usize, end: usize) -> Option<Match> {
    Some(Match {
        start,
        end,
        body: None,
        spent: true,
    })
}

const PEM_LABELS: [&str; 6] = ["RSA ", "EC ", "DSA ", "OPENSSH ", "PGP ", "ENCRYPTED "];

impl Detector {
    /// The `kind` the reference names the detector by.
    pub fn kind(self) -> &'static str {
        match self {
            Detector::AwsAccessKeyId => "aws-access-key-id",
            Detector::OpenAiAnthropic => "openai-anthropic-key",
            Detector::Stripe => "stripe-key",
            Detector::GithubToken | Detector::GithubPat => "github-token",
            Detector::Slack | Detector::SlackApp => "slack-token",
            Detector::GoogleApiKey => "google-api-key",
            Detector::SendGrid => "sendgrid-api-key",
            Detector::Vault => "vault-token",
            Detector::PrivateKeyPem => "private-key-pem",
            Detector::Jwt => "jwt",
        }
    }

    /// The reference's pattern text, as `re.compile` is given it. The
    /// golden header pins the same texts, so a changed pattern fails the
    /// golden test even when no case's matches move.
    pub fn pattern(self) -> &'static str {
        match self {
            Detector::AwsAccessKeyId => r"\b(?:AKIA|ASIA)(?P<body>[0-9A-Z]{16})\b",
            Detector::OpenAiAnthropic => {
                r"\bsk-(?:(?:ant-|proj-)?(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,}\b|(?P<spent>[A-Za-z0-9_-]*))"
            }
            Detector::Stripe => r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{24,}\b",
            Detector::GithubToken => r"\b(?:ghp|gho|ghu|ghs|ghr)_(?P<body>[A-Za-z0-9]{36})\b",
            Detector::GithubPat => r"\bgithub_pat_(?P<body>[0-9A-Za-z_]{22,})\b",
            Detector::Slack => {
                r"\bxox[abceprs]-(?:(?=[0-9A-Za-z-]*[0-9])[0-9A-Za-z-]{10,}\b|(?P<spent>[0-9A-Za-z-]*))"
            }
            Detector::SlackApp => {
                r"\bxapp-(?:(?=[0-9A-Za-z-]*[0-9])[0-9A-Za-z-]{10,}\b|(?P<spent>[0-9A-Za-z-]*))"
            }
            Detector::GoogleApiKey => r"\bAIza[0-9A-Za-z_-]{35}\b",
            Detector::SendGrid => r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b",
            Detector::Vault => r"\bhv[sbr]\.[A-Za-z0-9_-]{24,}\b",
            Detector::PrivateKeyPem => {
                r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----(?=[ \t]*\r?\n(?:[A-Za-z0-9+/=_-]{16,}|(?:Proc-Type|DEK-Info):))"
            }
            Detector::Jwt => {
                r"\beyJ(?:[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b|(?P<spent>[A-Za-z0-9_-]*))"
            }
        }
    }

    /// The pattern's `match` at `p` (anchored there), or None.
    fn match_at(self, t: &[u32], p: usize) -> Option<Match> {
        if self != Detector::PrivateKeyPem && !boundary(t, p) {
            return None;
        }
        match self {
            Detector::AwsAccessKeyId => {
                if !(lit(t, p, "AKIA") || lit(t, p, "ASIA")) {
                    return None;
                }
                let e = exactly(t, p + 4, 16, digit_upper)?;
                boundary(t, e).then_some(())?;
                hit(p, e, Some((p + 4, e)))
            }
            Detector::OpenAiAnthropic => {
                if !lit(t, p, "sk-") {
                    return None;
                }
                let q = p + 3;
                let e = run(t, q, c_class);
                // `(?:ant-|proj-)?` needs no start of its own: both prefixes
                // lie inside the run, hold no uppercase letter or digit, and
                // only raise the minimum end, so the largest boundary from
                // after one is the largest from `q`, and the span is the same.
                if any_in(t, q, e, upper) && any_in(t, q, e, digit) {
                    if let Some(k) = greedy_to_boundary(t, q, e, 20) {
                        return hit(p, k, None);
                    }
                }
                spent(p, e)
            }
            Detector::Stripe => {
                let first = t[p];
                if !(first == u32::from(b's') || first == u32::from(b'r')) || !lit(t, p + 1, "k_") {
                    return None;
                }
                if !(lit(t, p + 3, "live_") || lit(t, p + 3, "test_")) {
                    return None;
                }
                let s = p + 8;
                let k = greedy_to_boundary(t, s, run(t, s, alnum), 24)?;
                hit(p, k, None)
            }
            Detector::GithubToken => {
                if !["ghp_", "gho_", "ghu_", "ghs_", "ghr_"]
                    .iter()
                    .any(|x| lit(t, p, x))
                {
                    return None;
                }
                let e = exactly(t, p + 4, 36, alnum)?;
                boundary(t, e).then_some(())?;
                hit(p, e, Some((p + 4, e)))
            }
            Detector::GithubPat => {
                if !lit(t, p, "github_pat_") {
                    return None;
                }
                let s = p + 11;
                let k = greedy_to_boundary(t, s, run(t, s, alnum_under), 22)?;
                hit(p, k, Some((s, k)))
            }
            Detector::Slack | Detector::SlackApp => {
                let q = if self == Detector::Slack {
                    let ok = lit(t, p, "xox")
                        && p + 4 < t.len()
                        && b"abceprs".iter().any(|&b| t[p + 3] == u32::from(b))
                        && t[p + 4] == u32::from(b'-');
                    if !ok {
                        return None;
                    }
                    p + 5
                } else {
                    if !lit(t, p, "xapp-") {
                        return None;
                    }
                    p + 5
                };
                let e = run(t, q, d_class);
                if any_in(t, q, e, digit) {
                    if let Some(k) = greedy_to_boundary(t, q, e, 10) {
                        return hit(p, k, None);
                    }
                }
                spent(p, e)
            }
            Detector::GoogleApiKey => {
                if !lit(t, p, "AIza") {
                    return None;
                }
                let e = exactly(t, p + 4, 35, c_class)?;
                boundary(t, e).then_some(())?;
                hit(p, e, None)
            }
            Detector::SendGrid => {
                if !lit(t, p, "SG.") {
                    return None;
                }
                let a = exactly(t, p + 3, 22, c_class)?;
                if !lit(t, a, ".") {
                    return None;
                }
                let e = exactly(t, a + 1, 43, c_class)?;
                boundary(t, e).then_some(())?;
                hit(p, e, None)
            }
            Detector::Vault => {
                if !(lit(t, p, "hvs.") || lit(t, p, "hvb.") || lit(t, p, "hvr.")) {
                    return None;
                }
                let s = p + 4;
                let k = greedy_to_boundary(t, s, run(t, s, c_class), 24)?;
                hit(p, k, None)
            }
            Detector::PrivateKeyPem => {
                if !lit(t, p, "-----BEGIN ") {
                    return None;
                }
                let mut h = p + 11;
                if let Some(label) = PEM_LABELS.iter().find(|l| lit(t, h, l)) {
                    h += label.len();
                }
                if !lit(t, h, "PRIVATE KEY-----") {
                    return None;
                }
                let end = h + 16;
                let mut i = end;
                while i < t.len() && (t[i] == 0x20 || t[i] == 0x09) {
                    i += 1;
                }
                if i < t.len() && t[i] == 0x0D {
                    i += 1;
                }
                if !(i < t.len() && t[i] == 0x0A) {
                    return None;
                }
                i += 1;
                let material = exactly(t, i, 16, pem_body).is_some();
                (material || lit(t, i, "Proc-Type:") || lit(t, i, "DEK-Info:")).then_some(())?;
                hit(p, end, None)
            }
            Detector::Jwt => {
                if !lit(t, p, "eyJ") {
                    return None;
                }
                let q = p + 3;
                let e1 = run(t, q, c_class);
                let token = || {
                    if e1 < q + 8 || !lit(t, e1, ".") {
                        return None;
                    }
                    let e2 = run(t, e1 + 1, c_class);
                    if e2 < e1 + 9 || !lit(t, e2, ".") {
                        return None;
                    }
                    greedy_to_boundary(t, e2 + 1, run(t, e2 + 1, c_class), 8)
                };
                match token() {
                    Some(k) => hit(p, k, None),
                    None => spent(p, e1),
                }
            }
        }
    }

    /// `regex.finditer(text)`, every match in order.
    pub fn find_iter(self, text: &[u32]) -> Vec<Match> {
        let mut out = Vec::new();
        let mut p = 0;
        while p < text.len() {
            match self.match_at(text, p) {
                Some(m) => {
                    p = m.end;
                    out.push(m);
                }
                None => p += 1,
            }
        }
        out
    }
}

/// `_is_masked_token`: the body names fewer than five distinct characters.
pub fn is_masked_token(text: &[u32], m: &Match) -> bool {
    match m.body {
        Some((s, e)) => {
            // A body span outside `text` (a hand-built `Match`, or one from
            // another text) names no body here: not a mask, and no panic.
            let Some(body) = text.get(s..e) else {
                return false;
            };
            let mut seen: Vec<u32> = Vec::new();
            for &c in body {
                if !seen.contains(&c) {
                    seen.push(c);
                    if seen.len() >= MIN_BODY_DIVERSITY {
                        return false;
                    }
                }
            }
            true
        }
        None => false,
    }
}

/// `_is_spent_run`.
pub fn is_spent_run(m: &Match) -> bool {
    m.spent
}

/// The first loop of `_merged_secret_spans`: every detector's matches in
/// `_PREFIXED_DETECTORS`' order, spent runs and masks left out, as
/// `(start, end, kind)`.
pub fn prefixed_spans(text: &[u32]) -> Vec<(usize, usize, &'static str)> {
    let mut out = Vec::new();
    for d in DETECTORS {
        for m in d.find_iter(text) {
            if !is_spent_run(&m) && !is_masked_token(text, &m) {
                out.push((m.start, m.end, d.kind()));
            }
        }
    }
    out
}

/// A convenience for callers holding a `&str`.
pub fn prefixed_spans_str(text: &str) -> Vec<(usize, usize, &'static str)> {
    let cps: PyStr = text.chars().map(u32::from).collect();
    prefixed_spans(&cps)
}
