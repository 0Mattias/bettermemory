//! Where git's reader of remotes dies on one config variable, as the
//! reference reads it: `src/bettermemory/gitconfig.py` lines 713-877
//! (`_remote_rule`, `refname_valid`, `refspec_valid` and their sets).
//!
//! Pure over a name and a value. A rule that accepts gives `None`, as the
//! reference does; one that refuses gives the reference's worded reason.

use super::{git_bool, Name};

/// "no boolean": a boolean key whose value `git_bool` refuses.
pub const NO_BOOLEAN: &str = "no boolean";
/// "no value": a string key with no `=`.
pub const NO_VALUE: &str = "no value";
/// "an empty branch name": a `branch ""` section.
pub const EMPTY_BRANCH: &str = "an empty branch name";
/// A negative refspec of 40 or 64 hex digits (`refspec_valid` gives None).
pub const OBJECT_FORMAT: &str = "a refspec whose reading turns on the object format";
/// A refspec `parse_refspec` refuses.
pub const BAD_REFSPEC: &str = "a refspec git refuses";

const REMOTE_STRINGS: &[&str] = &[
    "url",
    "pushurl",
    "push",
    "fetch",
    "receivepack",
    "uploadpack",
    "tagopt",
    "proxy",
    "proxyauthmethod",
    "vcs",
    "serveroption",
    "followremotehead",
];
const REMOTE_BOOLS: &[&str] = &[
    "mirror",
    "skipdefaultupdate",
    "skipfetchall",
    "prune",
    "prunetags",
];
const BRANCH_STRINGS: &[&str] = &["remote", "pushremote", "merge"];
const URL_STRINGS: &[&str] = &["insteadof", "pushinsteadof"];

/// Why git's reader of remotes (or of promisor remotes) dies on `name`
/// set to `value`, or `None` where it does not (`_remote_rule`, line 744).
/// `value` is the value as `_parse_config` keeps it (cut at a NUL).
pub fn remote_rule(name: &Name, value: Option<&[u8]>) -> Option<&'static str> {
    let section = name.section.as_str();
    let key = name.key.as_str();
    let sub = name.subsection.as_deref();
    if section == "remote" && key == "promisor" {
        return git_bool(value).is_none().then_some(NO_BOOLEAN);
    }
    if section == "remote" && key == "partialclonefilter" {
        let named_slash = sub.is_some_and(|s| s.starts_with(b"/"));
        return (value.is_none() && !named_slash).then_some(NO_VALUE);
    }
    if section == "branch" {
        if sub == Some(&b""[..]) {
            return Some(EMPTY_BRANCH);
        }
        if sub.is_some() && BRANCH_STRINGS.contains(&key) && value.is_none() {
            return Some(NO_VALUE);
        }
        return None;
    }
    if section == "url" {
        if sub.is_some() && URL_STRINGS.contains(&key) && value.is_none() {
            return Some(NO_VALUE);
        }
        return None;
    }
    if section != "remote" {
        return None;
    }
    let Some(sub) = sub else {
        return (key == "pushdefault" && value.is_none()).then_some(NO_VALUE);
    };
    if sub.starts_with(b"/") {
        return None;
    }
    if REMOTE_BOOLS.contains(&key) {
        return git_bool(value).is_none().then_some(NO_BOOLEAN);
    }
    if REMOTE_STRINGS.contains(&key) && value.is_none() {
        return Some(NO_VALUE);
    }
    if key == "fetch" || key == "push" {
        if let Some(value) = value {
            return match refspec_valid(value, key == "fetch") {
                None => Some(OBJECT_FORMAT),
                Some(false) => Some(BAD_REFSPEC),
                Some(true) => None,
            };
        }
    }
    None
}

fn refname_bad(byte: u8) -> bool {
    byte < 0x20 || matches!(byte, 0x7f | b' ' | b'~' | b'^' | b':' | b'?' | b'[' | b'\\')
}

fn contains(hay: &[u8], needle: &[u8]) -> bool {
    hay.windows(needle.len()).any(|w| w == needle)
}

/// git's `check_refname_format` as the reference reads it
/// (`refname_valid`, line 806): one `*` where `pattern`, one component
/// only where `onelevel`.
pub fn refname_valid(name: &[u8], pattern: bool, onelevel: bool) -> bool {
    if name == b"@" || name.is_empty() || name.ends_with(b".") {
        return false;
    }
    if contains(name, b"..") || contains(name, b"@{") {
        return false;
    }
    let stars = name.iter().filter(|&&b| b == b'*').count();
    if stars > usize::from(pattern) {
        return false;
    }
    if name.iter().any(|&b| refname_bad(b)) {
        return false;
    }
    let parts: Vec<&[u8]> = name.split(|&b| b == b'/').collect();
    if !onelevel && parts.len() < 2 {
        return false;
    }
    parts
        .iter()
        .all(|part| !part.is_empty() && !part.starts_with(b".") && !part.ends_with(b".lock"))
}

/// git's `parse_refspec` as the reference reads it (`refspec_valid`, line
/// 831): whether git takes `spec` as a fetch (`fetch`) or push refspec;
/// `None` for a negative refspec of 40 or 64 hex digits.
pub fn refspec_valid(spec: &[u8], fetch: bool) -> Option<bool> {
    let mut lhs = spec;
    let mut negative = false;
    if lhs.first() == Some(&b'+') {
        lhs = &lhs[1..];
    } else if lhs.first() == Some(&b'^') {
        negative = true;
        lhs = &lhs[1..];
    }
    let colon = lhs.iter().rposition(|&b| b == b':');
    let rhs = colon.map(|c| &lhs[c + 1..]);
    if negative && rhs.is_some() {
        return Some(false);
    }
    if !fetch && colon == Some(0) && rhs == Some(&b""[..]) {
        return Some(true);
    }
    let mut src = match colon {
        None => lhs,
        Some(c) => &lhs[..c],
    };
    let mut glob = rhs.is_some_and(|r| r.contains(&b'*'));
    if !src.is_empty() && src.contains(&b'*') {
        if (rhs.is_some() && !glob) || (rhs.is_none() && !negative && fetch) {
            return Some(false);
        }
        glob = true;
    } else if rhs.is_some() && glob {
        return Some(false);
    }
    if src == b"@" {
        src = b"HEAD";
    }
    let valid = |name: &[u8]| refname_valid(name, glob, true);
    let hexed = (src.len() == 40 || src.len() == 64) && src.iter().all(u8::is_ascii_hexdigit);
    if negative {
        if src.is_empty() {
            return Some(false);
        }
        if hexed {
            return None;
        }
        return Some(valid(src));
    }
    if fetch {
        if !src.is_empty() && !hexed && !valid(src) {
            return Some(false);
        }
        return Some(rhs.is_none_or(|r| r.is_empty() || valid(r)));
    }
    if !src.is_empty() && glob && !valid(src) {
        return Some(false);
    }
    match rhs {
        None => Some(valid(src)),
        Some(r) => Some(!r.is_empty() && valid(r)),
    }
}
