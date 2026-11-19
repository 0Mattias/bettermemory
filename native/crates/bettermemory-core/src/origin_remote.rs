//! `origin_remote`: a remote URL read into the `(host, owner, name)`
//! triple `origin.repos_match` compares (`_parse_remote`), and the vendor
//! shapes folded onto one triple (`_canonicalize`, `_canonicalize_azure`).
//!
//! Texts are code points ([`PyStr`]), since a record's remote is a Python
//! `str` and may hold a lone surrogate. The scp-like pattern
//! `_SSH_REMOTE_RE` is matched by hand ([`scp_match`]), and
//! `urllib.parse.urlparse` with its `hostname` is reproduced as CPython
//! 3.13.13 has it ([`urlparse`]), with `ipaddress`'s address grammar for
//! a bracketed host. `str.strip`, `str.lower` and NFKC read the crate's
//! Unicode tables.

use crate::pyvalue::PyStr;
use crate::unicode::{is_space, lower, nfkc_into};

/// A parsed remote: `(host, owner, name)`.
pub type Triple = (PyStr, PyStr, PyStr);

const SLASH: u32 = '/' as u32;
const COLON: u32 = ':' as u32;
const AT: u32 = '@' as u32;
const NL: u32 = '\n' as u32;

fn cps(text: &str) -> Vec<u32> {
    text.chars().map(u32::from).collect()
}

fn find(text: &[u32], cp: u32, from: usize) -> Option<usize> {
    text[from..].iter().position(|&c| c == cp).map(|i| i + from)
}

fn has(text: &[u32], cp: u32) -> bool {
    text.contains(&cp)
}

fn contains(text: &[u32], part: &[u32]) -> bool {
    part.is_empty() || text.windows(part.len()).any(|w| w == part)
}

fn eq(text: &[u32], ascii: &str) -> bool {
    text.len() == ascii.len() && text.iter().zip(ascii.bytes()).all(|(&c, b)| c == b as u32)
}

fn ends_with(text: &[u32], ascii: &str) -> bool {
    text.len() >= ascii.len() && eq(&text[text.len() - ascii.len()..], ascii)
}

fn starts_with(text: &[u32], ascii: &str) -> bool {
    text.len() >= ascii.len() && eq(&text[..ascii.len()], ascii)
}

/// `text.strip(chars)` for a set of code points.
fn strip_by(text: &[u32], gone: impl Fn(u32) -> bool) -> &[u32] {
    let start = text.iter().position(|&c| !gone(c)).unwrap_or(text.len());
    let end = text
        .iter()
        .rposition(|&c| !gone(c))
        .map_or(start, |i| i + 1);
    &text[start..end]
}

fn rstrip_slash(text: &[u32]) -> &[u32] {
    let end = text.iter().rposition(|&c| c != SLASH).map_or(0, |i| i + 1);
    &text[..end]
}

fn remove_suffix<'a>(text: &'a [u32], ascii: &str) -> &'a [u32] {
    if ends_with(text, ascii) {
        &text[..text.len() - ascii.len()]
    } else {
        text
    }
}

/// `text.partition(sep)` for one code point: before, found, after.
fn partition(text: &[u32], sep: u32) -> (&[u32], bool, &[u32]) {
    match find(text, sep, 0) {
        Some(i) => (&text[..i], true, &text[i + 1..]),
        None => (text, false, &[]),
    }
}

/// `text.rpartition(sep)[2]`.
fn after_last(text: &[u32], sep: u32) -> &[u32] {
    match text.iter().rposition(|&c| c == sep) {
        Some(i) => &text[i + 1..],
        None => text,
    }
}

fn split(text: &[u32], sep: u32) -> Vec<&[u32]> {
    text.split(|&c| c == sep).collect()
}

/// What `_SSH_REMOTE_RE.match` captures, as ranges of `url`:
/// `^(?:[a-zA-Z0-9_.+-]+@)?([^/:@]{2,}):(?!//)/?(?:([^/]+)/)?(.+?)(?:\.git)?/?$`.
///
/// Each part is decided by the pattern's backtracking order. The user
/// prefix holds no `@`, so it is the leading run of its class when an `@`
/// follows, and without it the host would run into that `@`. The host
/// holds no `:`, so it is the run of `[^/:@]` up to the first of the
/// three. The tail `(.+?)(?:\.git)?/?$` matches from `x` exactly when the
/// rest is non-empty and holds no newline (`.` matches none, and the
/// stripped text cannot end in one, so `$` is the end), and the lazy name
/// ends at the first of `.git/`, `.git`, `/` or the end that leaves it
/// non-empty. The optional slash is tried first, then the owner (a run
/// of `[^/]` up to a `/`), then none.
pub fn scp_match(url: &[u32]) -> Option<(PyStr, Option<PyStr>, PyStr)> {
    let user = |c: u32| {
        c < 0x80 && {
            let b = c as u8;
            b.is_ascii_alphanumeric() || b"_.+-".contains(&b)
        }
    };
    let run = url.iter().take_while(|&&c| user(c)).count();
    let start = if run > 0 && url.get(run) == Some(&AT) {
        run + 1
    } else {
        0
    };
    let host_end = start
        + url[start..]
            .iter()
            .take_while(|&&c| c != SLASH && c != COLON && c != AT)
            .count();
    if host_end - start < 2 || url.get(host_end) != Some(&COLON) {
        return None;
    }
    let c = host_end + 1;
    if starts_with(&url[c..], "//") {
        return None;
    }
    let tail = |x: usize| -> Option<PyStr> {
        if x >= url.len() || has(&url[x..], NL) {
            return None;
        }
        let n = url.len();
        let mut ends = Vec::with_capacity(4);
        if ends_with(url, ".git/") {
            ends.push(n - 5);
        }
        if ends_with(url, ".git") {
            ends.push(n - 4);
        }
        if ends_with(url, "/") {
            ends.push(n - 1);
        }
        ends.push(n);
        let e = ends.into_iter().find(|&e| e > x)?;
        Some(url[x..e].to_vec())
    };
    let from = |p: usize| -> Option<(Option<PyStr>, PyStr)> {
        if let Some(q) = find(url, SLASH, p).filter(|&q| q > p) {
            if let Some(name) = tail(q + 1) {
                return Some((Some(url[p..q].to_vec()), name));
            }
        }
        tail(p).map(|name| (None, name))
    };
    let host = url[start..host_end].to_vec();
    let found = if url.get(c) == Some(&SLASH) {
        from(c + 1).or_else(|| from(c))
    } else {
        from(c)
    };
    found.map(|(owner, name)| (host, owner, name))
}

/// The `ValueError` `urlparse` raises, by its message.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UrlError {
    /// "Invalid IPv6 URL".
    Ipv6,
    /// "IPvFuture address is invalid".
    Future,
    /// "An IPv4 address cannot be in brackets".
    Ipv4,
    /// "... does not appear to be an IPv4 or IPv6 address".
    Address,
    /// "netloc ... contains invalid characters under NFKC normalization".
    Nfkc,
}

/// `urlparse(url)`'s six parts.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Parsed {
    pub scheme: PyStr,
    pub netloc: PyStr,
    pub path: PyStr,
    pub params: PyStr,
    pub query: PyStr,
    pub fragment: PyStr,
}

const SCHEME_CHARS: &str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-.";
const USES_PARAMS: [&str; 16] = [
    "", "ftp", "hdl", "prospero", "http", "imap", "https", "shttp", "rtsp", "rtsps", "rtspu",
    "sip", "sips", "mms", "sftp", "tel",
];

/// `urllib.parse.urlparse(url)` (scheme `''`, fragments allowed), as
/// CPython 3.13.13's `urlsplit` and `_splitparams` take it apart.
pub fn urlparse(url: &[u32]) -> Result<Parsed, UrlError> {
    // `url.lstrip(_WHATWG_C0_CONTROL_OR_SPACE)`, then tab, CR and LF removed.
    let lead = url.iter().take_while(|&&c| c <= 0x20).count();
    let mut url: PyStr = url[lead..]
        .iter()
        .copied()
        .filter(|&c| c != 0x09 && c != 0x0D && c != NL)
        .collect();
    let mut scheme = PyStr::new();
    if let Some(i) = find(&url, COLON, 0).filter(|&i| i > 0) {
        let first = url[0];
        if first < 0x80
            && (first as u8).is_ascii_alphabetic()
            && url[..i]
                .iter()
                .all(|&c| c < 0x80 && SCHEME_CHARS.contains(c as u8 as char))
        {
            scheme = url[..i]
                .iter()
                .map(|&c| (c as u8).to_ascii_lowercase() as u32)
                .collect();
            url.drain(..=i);
        }
    }
    let mut netloc = PyStr::new();
    if starts_with(&url, "//") {
        let delim = url[2..]
            .iter()
            .position(|&c| c == SLASH || c == '?' as u32 || c == '#' as u32)
            .map_or(url.len(), |i| i + 2);
        netloc = url[2..delim].to_vec();
        url.drain(..delim);
        let (open, close) = (has(&netloc, '[' as u32), has(&netloc, ']' as u32));
        if open != close {
            return Err(UrlError::Ipv6);
        }
        if open {
            check_bracketed_netloc(&netloc)?;
        }
    }
    let mut fragment = PyStr::new();
    if let Some(i) = find(&url, '#' as u32, 0) {
        fragment = url.split_off(i + 1);
        url.pop();
    }
    let mut query = PyStr::new();
    if let Some(i) = find(&url, '?' as u32, 0) {
        query = url.split_off(i + 1);
        url.pop();
    }
    check_netloc(&netloc)?;
    let semi = ';' as u32;
    let mut params = PyStr::new();
    if USES_PARAMS.iter().any(|s| eq(&scheme, s)) && has(&url, semi) {
        // `_splitparams`: the first ';' after the last '/', else the first.
        let from = url.iter().rposition(|&c| c == SLASH).unwrap_or(0);
        if let Some(i) = find(&url, semi, from) {
            params = url.split_off(i + 1);
            url.pop();
        }
    }
    Ok(Parsed {
        scheme,
        netloc,
        path: url,
        params,
        query,
        fragment,
    })
}

/// `_checknetloc`: a netloc whose NFKC form (without `@`, `:`, `#` and
/// `?`) changes and then holds one of `/?#@:` is refused.
fn check_netloc(netloc: &[u32]) -> Result<(), UrlError> {
    if netloc.iter().all(|&c| c < 0x80) {
        return Ok(());
    }
    let n: PyStr = netloc
        .iter()
        .copied()
        .filter(|&c| !"@:#?".contains_cp(c))
        .collect();
    let mut n2 = Vec::new();
    nfkc_into(&n, &mut n2);
    if n == n2 {
        return Ok(());
    }
    if n2.iter().any(|&c| "/?#@:".contains_cp(c)) {
        return Err(UrlError::Nfkc);
    }
    Ok(())
}

trait ContainsCp {
    fn contains_cp(&self, cp: u32) -> bool;
}

impl ContainsCp for str {
    fn contains_cp(&self, cp: u32) -> bool {
        self.chars().any(|c| u32::from(c) == cp)
    }
}

/// `_check_bracketed_netloc`, which mirrors `_hostinfo`'s split.
fn check_bracketed_netloc(netloc: &[u32]) -> Result<(), UrlError> {
    let host_port = after_last(netloc, AT);
    let (before, open, bracketed) = partition(host_port, '[' as u32);
    let hostname = if open {
        if !before.is_empty() {
            return Err(UrlError::Ipv6);
        }
        let (hostname, _, port) = partition(bracketed, ']' as u32);
        if !port.is_empty() && port[0] != COLON {
            return Err(UrlError::Ipv6);
        }
        hostname
    } else {
        partition(host_port, COLON).0
    };
    check_bracketed_host(hostname)
}

/// `_check_bracketed_host`: `\Av[a-fA-F0-9]+\..+\Z` for a host that
/// begins with `v`, else `ipaddress.ip_address`, which must be IPv6.
fn check_bracketed_host(host: &[u32]) -> Result<(), UrlError> {
    let hex = |c: u32| c < 0x80 && (c as u8).is_ascii_hexdigit();
    if host.first() == Some(&('v' as u32)) {
        let run = host[1..].iter().take_while(|&&c| hex(c)).count();
        let rest = &host[1 + run..];
        let ok = run > 0
            && rest.first() == Some(&('.' as u32))
            && rest.len() > 1
            && !has(&rest[1..], NL);
        return if ok { Ok(()) } else { Err(UrlError::Future) };
    }
    if !has(host, SLASH) && ipv4(host) {
        return Err(UrlError::Ipv4);
    }
    if ipv6(host) {
        Ok(())
    } else {
        Err(UrlError::Address)
    }
}

/// `IPv4Address._ip_int_from_string` succeeds.
fn ipv4(text: &[u32]) -> bool {
    let octets = split(text, '.' as u32);
    !text.is_empty()
        && octets.len() == 4
        && octets.iter().all(|o| {
            !o.is_empty()
                && o.len() <= 3
                && o.iter().all(|&c| (0x30..=0x39).contains(&c))
                && (o.len() == 1 || o[0] != 0x30)
                && o.iter().fold(0u32, |n, &c| n * 10 + (c - 0x30)) <= 255
        })
}

/// `IPv6Address(text)` succeeds: the scope split, then
/// `_ip_int_from_string` step by step.
fn ipv6(text: &[u32]) -> bool {
    if has(text, SLASH) {
        return false;
    }
    let (addr, sep, scope) = partition(text, '%' as u32);
    if sep && (scope.is_empty() || has(scope, '%' as u32)) {
        return false;
    }
    if addr.is_empty() || addr.len() > 45 {
        return false;
    }
    // `split(':', maxsplit=9)`.
    let mut parts: Vec<&[u32]> = Vec::new();
    let mut rest = addr;
    while parts.len() < 9 {
        match find(rest, COLON, 0) {
            Some(i) => {
                parts.push(&rest[..i]);
                rest = &rest[i + 1..];
            }
            None => break,
        }
    }
    parts.push(rest);
    if parts.len() < 3 {
        return false;
    }
    const ZERO: &[u32] = &[0x30];
    if has(parts[parts.len() - 1], '.' as u32) {
        if !ipv4(parts.pop().unwrap_or_default()) {
            return false;
        }
        parts.extend([ZERO, ZERO]);
    }
    if parts.len() > 9 {
        return false;
    }
    let mut skip = None;
    for (i, part) in parts.iter().enumerate().take(parts.len() - 1).skip(1) {
        if part.is_empty() {
            if skip.is_some() {
                return false;
            }
            skip = Some(i);
        }
    }
    let (hi, lo) = match skip {
        Some(s) => {
            let (mut hi, mut lo) = (s, parts.len() - s - 1);
            if parts[0].is_empty() {
                hi -= 1;
                if hi > 0 {
                    return false;
                }
            }
            if parts[parts.len() - 1].is_empty() {
                lo -= 1;
                if lo > 0 {
                    return false;
                }
            }
            if 8 < hi + lo + 1 {
                return false;
            }
            (hi, lo)
        }
        None => {
            if parts.len() != 8 || parts[0].is_empty() || parts[7].is_empty() {
                return false;
            }
            (8, 0)
        }
    };
    let hextet = |h: &[u32]| {
        !h.is_empty()
            && h.len() <= 4
            && h.iter().all(|&c| c < 0x80 && (c as u8).is_ascii_hexdigit())
    };
    parts[..hi].iter().all(|h| hextet(h)) && parts[parts.len() - lo..].iter().all(|h| hextet(h))
}

/// `ParseResult.hostname`: the host of `_hostinfo`, lowered before any
/// `%` (a zone keeps its case), or None where it is empty.
pub fn hostname(netloc: &[u32]) -> Option<PyStr> {
    let host_port = after_last(netloc, AT);
    let (_, open, bracketed) = partition(host_port, '[' as u32);
    let host = if open {
        partition(bracketed, ']' as u32).0
    } else {
        partition(host_port, COLON).0
    };
    if host.is_empty() {
        return None;
    }
    let (name, percent, zone) = partition(host, '%' as u32);
    let mut out = lower(name);
    if percent {
        out.push('%' as u32);
        out.extend_from_slice(zone);
    }
    Some(out)
}

const SCHEMES: [&str; 6] = [
    "http://",
    "https://",
    "git://",
    "ssh://",
    "git+ssh://",
    "ssh+git://",
];

/// `_VENDOR_ROUTE_PREFIXES`: a routing prefix and the host substring it
/// is stripped on.
fn vendor_hint(owner_lower: &[u32]) -> Option<&'static str> {
    if eq(owner_lower, "scm") {
        Some("bitbucket")
    } else if eq(owner_lower, "a") {
        Some("gerrit")
    } else {
        None
    }
}

/// `_parse_remote(url)`: None where the reference returns None.
pub fn parse_remote(url: &[u32]) -> Option<Triple> {
    let url = strip_by(url, is_space);
    if url.is_empty() {
        return None;
    }
    if let Some((host, owner, name)) = scp_match(url) {
        let name = rstrip_slash(remove_suffix(&name, ".git")).to_vec();
        if name.is_empty() {
            return None;
        }
        return Some(canonicalize(&host, &owner.unwrap_or_default(), &name));
    }
    if !SCHEMES.iter().any(|s| starts_with(url, s)) {
        return None;
    }
    let parsed = urlparse(url).ok()?;
    let host = hostname(&parsed.netloc).unwrap_or_default();
    let path = strip_by(&parsed.path, |c| c == SLASH);
    if host.is_empty() || path.is_empty() {
        return None;
    }
    let path = rstrip_slash(remove_suffix(path, ".git"));
    if path.is_empty() {
        return None;
    }
    let (mut owner, found, mut name) = partition(path, SLASH);
    if !found || name.is_empty() {
        // A single path segment: an ownerless repository at the root.
        (owner, name) = (&[], owner);
    } else if (eq(&parsed.scheme, "http") || eq(&parsed.scheme, "https"))
        && has(name, SLASH)
        && vendor_hint(&lower(owner)).is_some_and(|hint| contains(&lower(&host), &cps(hint)))
    {
        (owner, _, name) = partition(name, SLASH);
    }
    Some(canonicalize(&host, owner, name))
}

/// `_HOST_ALIASES`: the first-party SSH-over-443 hosts.
fn alias(host_lower: &[u32]) -> Option<&'static str> {
    if eq(host_lower, "ssh.github.com") {
        Some("github.com")
    } else if eq(host_lower, "altssh.gitlab.com") {
        Some("gitlab.com")
    } else {
        None
    }
}

/// `_canonicalize(host, owner, name)`.
pub fn canonicalize(host: &[u32], owner: &[u32], name: &[u32]) -> Triple {
    let h = lower(host);
    // An alias is lowercase already: its lowering is itself.
    let (host, h) = match alias(&h) {
        Some(a) => (cps(a), cps(a)),
        None => (host.to_vec(), h),
    };
    if let Some(azure) = azure_lowered(&h, owner, name) {
        return azure;
    }
    (host, owner.to_vec(), name.to_vec())
}

fn azure(org: &[u32], project: &[u32], repo: &[u32]) -> Triple {
    let mut name = project.to_vec();
    name.push(SLASH);
    name.extend_from_slice(repo);
    (cps("dev.azure.com"), org.to_vec(), name)
}

/// `_canonicalize_azure(host, owner, name)`: the official Azure DevOps
/// clone shapes onto `('dev.azure.com', org, '{project}/{repo}')`.
pub fn canonicalize_azure(host: &[u32], owner: &[u32], name: &[u32]) -> Option<Triple> {
    azure_lowered(&lower(host), owner, name)
}

/// `_canonicalize_azure` given `host.lower()`, which is all it reads of
/// the host.
fn azure_lowered(h: &[u32], owner: &[u32], name: &[u32]) -> Option<Triple> {
    if eq(h, "ssh.dev.azure.com") || eq(h, "vs-ssh.visualstudio.com") {
        let segs = split(name, SLASH);
        if eq(&lower(owner), "v3") && segs.len() == 3 && segs.iter().all(|s| !s.is_empty()) {
            return Some(azure(segs[0], segs[1], segs[2]));
        }
        return None;
    }
    let git = |s: &[u32]| eq(s, "_git");
    if eq(h, "dev.azure.com") {
        let segs = split(name, SLASH);
        if segs.len() == 3
            && git(segs[1])
            && !owner.is_empty()
            && !segs[0].is_empty()
            && !segs[2].is_empty()
        {
            return Some(azure(owner, segs[0], segs[2]));
        }
        return None;
    }
    if ends_with(h, ".visualstudio.com") {
        let org = &h[..h.len() - ".visualstudio.com".len()];
        if org.is_empty() || has(org, '.' as u32) {
            return None;
        }
        let mut segs = Vec::new();
        if !owner.is_empty() {
            segs.push(owner);
        }
        segs.extend(split(name, SLASH));
        if segs.first().is_some_and(|s| eq(s, "DefaultCollection")) {
            segs.remove(0);
        }
        if segs.len() == 3 && git(segs[1]) && !segs[0].is_empty() && !segs[2].is_empty() {
            return Some(azure(org, segs[0], segs[2]));
        }
        return None;
    }
    None
}
