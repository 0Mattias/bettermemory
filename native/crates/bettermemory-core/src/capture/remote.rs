//! `location.remote_key` (`location.py` 366-405 at ececad8c): the key two
//! URLs of one hosted repository share, and the decision that a URL names
//! a local repository instead.
//!
//! The local branch is not ported: the reference joins a relative path to
//! the repository's top level with the host's `os.path`, resolves it with
//! `os.path.realpath` (which reads the file system) and reads a
//! ``file://`` URL with `_file_url_path`, which depends on the host's
//! separator and on `urllib.request.url2pathname` (changed in Python
//! 3.14). Here a local URL is [`RemoteKey::Local`], carrying the stripped
//! text for the unit that reads the file system.

use crate::pyvalue::PyStr;
use crate::unicode;
use crate::unicode::py_strip;

/// What `remote_key` makes of a URL.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RemoteKey {
    /// ``host/path``, lower case, slashes trimmed, one ``.git`` dropped.
    Hosted(PyStr),
    /// The reference keys it ``local:`` and the real path: `text` is the
    /// URL after `str.strip()`, unchanged (no base joined, no path read);
    /// `file_url` is whether it starts ``file://`` in any case.
    Local { text: PyStr, file_url: bool },
}

const NL: u32 = b'\n' as u32;

fn is(c: u32, b: u8) -> bool {
    c == u32::from(b)
}

/// `[a-z]` under `re.IGNORECASE`: ASCII letters and the four characters
/// whose case maps reach them (U+0130, U+0131, U+017F, U+212A).
fn letter(c: u32) -> bool {
    char::from_u32(c).is_some_and(|ch| ch.is_ascii_alphabetic())
        || matches!(c, 0x130 | 0x131 | 0x17F | 0x212A)
}

/// `[a-z0-9+.-]` under `re.IGNORECASE`.
fn scheme_char(c: u32) -> bool {
    letter(c)
        || char::from_u32(c).is_some_and(|ch| ch.is_ascii_digit())
        || is(c, b'+')
        || is(c, b'.')
        || is(c, b'-')
}

/// The run of `t[from..]` whose characters satisfy `ok`.
fn run(t: &[u32], from: usize, ok: impl Fn(u32) -> bool) -> usize {
    t[from..].iter().take_while(|&&c| ok(c)).count()
}

/// `(.+)$` from `at`: the rest, if non-empty and without a line feed.
fn rest_from(t: &[u32], at: usize) -> Option<usize> {
    (at < t.len() && !t[at..].contains(&NL)).then_some(at)
}

/// From an authority's host on: ``([^/:]+)(?::\d+)?/(.+)$``.
fn after_userinfo(t: &[u32], q: usize) -> Option<(usize, usize, usize)> {
    let host = run(t, q, |c| !is(c, b'/') && !is(c, b':'));
    if host == 0 {
        return None;
    }
    let h = q + host;
    let slash = match t.get(h) {
        Some(&c) if is(c, b':') => {
            let digits = run(t, h + 1, unicode::is_re_digit);
            let s = h + 1 + digits;
            if digits == 0 || !t.get(s).is_some_and(|&c| is(c, b'/')) {
                return None;
            }
            s
        }
        Some(&c) if is(c, b'/') => h,
        _ => return None,
    };
    rest_from(t, slash + 1).map(|r| (q, h, r))
}

/// `_SCHEME_URL`: (host start, host end, rest start).
fn scheme_url(t: &[u32]) -> Option<(usize, usize, usize)> {
    if !t.first().is_some_and(|&c| letter(c)) {
        return None;
    }
    let i = 1 + run(t, 1, scheme_char);
    let sep = [b':', b'/', b'/'];
    if t.len() < i + 3 || !t[i..i + 3].iter().zip(sep).all(|(&c, b)| is(c, b)) {
        return None;
    }
    let p = i + 3;
    let user = run(t, p, |c| !is(c, b'@') && !is(c, b'/'));
    if user > 0 && t.get(p + user).is_some_and(|&c| is(c, b'@')) {
        if let Some(found) = after_userinfo(t, p + user + 1) {
            return Some(found);
        }
    }
    after_userinfo(t, p)
}

/// `_SCP_URL`: ``^(?:[^@/]+@)?([^:/]+):(.+)$``.
fn scp_url(t: &[u32]) -> Option<(usize, usize, usize)> {
    let from = |q: usize| {
        let host = run(t, q, |c| !is(c, b':') && !is(c, b'/'));
        let h = q + host;
        if host == 0 || !t.get(h).is_some_and(|&c| is(c, b':')) {
            return None;
        }
        rest_from(t, h + 1).map(|r| (q, h, r))
    };
    let user = run(t, 0, |c| !is(c, b'@') && !is(c, b'/'));
    if user > 0 && t.get(user).is_some_and(|&c| is(c, b'@')) {
        if let Some(found) = from(user + 1) {
            return Some(found);
        }
    }
    from(0)
}

/// `remote_key(url)`: None for no URL or a blank one.
pub fn remote_key(url: Option<&[u32]>) -> Option<RemoteKey> {
    let text = py_strip(url?);
    if text.is_empty() {
        return None;
    }
    // `text.lower().startswith("file://")`: no character but an ASCII one
    // lowers to these, and U+0130's lowering puts a combining dot after
    // its "i", so comparing the first seven ASCII-insensitively is exact.
    let file_url = text.len() >= 7
        && text[..7].iter().zip("file://".bytes()).all(|(&c, b)| {
            char::from_u32(c).is_some_and(|ch| ch.to_ascii_lowercase() == char::from(b))
        });
    let local = || {
        Some(RemoteKey::Local {
            text: text.to_vec(),
            file_url,
        })
    };
    if file_url {
        return local();
    }
    let (q, h, r) = match scheme_url(text) {
        Some(found) => found,
        None => {
            let scp = scp_url(text);
            let drive = text.len() > 1 && is(text[1], b':') && unicode::is_alpha(text[0]);
            let starts = text
                .first()
                .is_some_and(|&c| is(c, b'/') || is(c, b'.') || is(c, b'\\'));
            match scp {
                Some(found) if !starts && !drive => found,
                _ => return local(),
            }
        }
    };
    let host = &text[q..h];
    let mut rest = &text[r..];
    while rest.first().is_some_and(|&c| is(c, b'/')) {
        rest = &rest[1..];
    }
    while rest.last().is_some_and(|&c| is(c, b'/')) {
        rest = &rest[..rest.len() - 1];
    }
    if rest.ends_with(&unicode::cps(".git")) {
        rest = &rest[..rest.len() - 4];
    }
    let mut key = unicode::lower(host);
    key.push(u32::from(b'/'));
    key.extend(unicode::lower(rest));
    Some(RemoteKey::Hosted(key))
}
