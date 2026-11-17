//! The path rules an include follows, as functions of text and of the
//! machine's facts given as data: `_git_absolute` (`gitconfig.py`
//! 612-617), `_native` (654-668), `_home` (671-680), `_user_home`
//! (578-591), `_interpolate` without `real_home` (535-575) and the
//! `os.path.dirname` and `os.path.join` `_include_target` calls (594-609),
//! for both path flavours.
//!
//! The reference's paths are `str`; here they are bytes, a `str` being
//! its `os.fsencode` (UTF-8, an undecodable byte as itself: Python's
//! `surrogateescape`). Every rule reads a separator, a colon, `~` or `%`,
//! which are ASCII and never part of a multi-byte character, except the
//! Windows drive test, which reads the first character ([`code_points`]).

use crate::fsdata::Flavour;
use crate::pyvalue::repr_str;
use crate::unicode;

/// The machine facts the path rules read.
pub struct Machine<'a> {
    pub flavour: Flavour,
    /// HOME as the environment holds it; None where it is unset.
    pub home: Option<Vec<u8>>,
    /// Windows only: `os.path.expanduser("~")` where HOME is unset or
    /// empty (USERPROFILE, else HOMEDRIVE and HOMEPATH); None where it
    /// leaves `~` as written.
    pub profile: Option<Vec<u8>>,
    /// `pwd.getpwnam(user).pw_dir`, None where there is no such account.
    /// Never asked under [`Flavour::Windows`], where git names no account.
    pub user_home: &'a dyn Fn(&[u8]) -> Option<Vec<u8>>,
}

/// The text's code points as Python decodes it (`os.fsdecode` on POSIX:
/// UTF-8, each undecodable byte as U+DC80 plus the byte).
pub fn code_points(text: &[u8]) -> Vec<u32> {
    let mut out = Vec::with_capacity(text.len());
    let mut rest = text;
    while !rest.is_empty() {
        match std::str::from_utf8(rest) {
            Ok(valid) => {
                out.extend(valid.chars().map(u32::from));
                break;
            }
            Err(e) => {
                let (valid, after) = rest.split_at(e.valid_up_to());
                out.extend(
                    std::str::from_utf8(valid)
                        .expect("valid prefix")
                        .chars()
                        .map(u32::from),
                );
                let bad = e.error_len().unwrap_or(after.len());
                out.extend(after[..bad].iter().map(|&b| 0xDC00 + u32::from(b)));
                rest = &after[bad..];
            }
        }
    }
    out
}

/// `repr` of the text as Python spells the `str` it decodes to; its
/// output is printable, so UTF-8.
pub fn repr_text(text: &[u8]) -> Vec<u8> {
    repr_str(&code_points(text))
        .iter()
        .filter_map(|&cp| char::from_u32(cp))
        .collect::<String>()
        .into_bytes()
}

fn is_sep(flavour: Flavour, b: u8) -> bool {
    b == b'/' || (flavour == Flavour::Windows && b == b'\\')
}

/// `_git_absolute`: a leading `/`; on Windows a leading `\` too, or a
/// letter and a colon, the letter by Python's `str.isalpha`, which takes
/// any Unicode letter (git takes ASCII only: a finding, reproduced).
pub fn git_absolute(flavour: Flavour, path: &[u8]) -> bool {
    match flavour {
        Flavour::Posix => path.first() == Some(&b'/'),
        Flavour::Windows => {
            if matches!(path.first(), Some(b'/' | b'\\')) {
                return true;
            }
            let cps = code_points(&path[..path.len().min(5)]);
            cps.get(1) == Some(&u32::from(b':'))
                && cps.first().is_some_and(|&c| unicode::is_alpha(c))
        }
    }
}

/// `_native`: the flavour's other separator written as its own.
pub fn native(flavour: Flavour, path: &[u8]) -> Vec<u8> {
    match flavour {
        Flavour::Posix => path.to_vec(),
        Flavour::Windows => path
            .iter()
            .map(|&b| if b == b'/' { b'\\' } else { b })
            .collect(),
    }
}

/// `_home`: HOME; on Windows, where it is unset or empty, the profile.
pub fn home(machine: &Machine) -> Option<Vec<u8>> {
    match machine.flavour {
        Flavour::Posix => machine.home.clone(),
        Flavour::Windows => match &machine.home {
            Some(h) if !h.is_empty() => Some(h.clone()),
            _ => machine.profile.clone(),
        },
    }
}

/// `_user_home`: none on Windows.
pub fn user_home(machine: &Machine, user: &[u8]) -> Option<Vec<u8>> {
    match machine.flavour {
        Flavour::Posix => (machine.user_home)(user),
        Flavour::Windows => None,
    }
}

/// The prefix git expands to the directory it runs from.
pub const RUNTIME_PREFIX: &[u8] = b"%(prefix)/";
/// What a git that leaves `%(prefix)/` as written answers for it.
pub const AS_WRITTEN: &[u8] = b"%(prefix)";

/// `_Overflow` of `_interpolate`: a `%(prefix)/` whose directory is not
/// known, where git reads what this reader does not.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PrefixUnknown;

/// `_interpolate(path, real_home=False, prefix=...)`: [`PrefixUnknown`] is the
/// reference's `_Overflow` (a `%(prefix)/` whose directory is not
/// known); `Ok(None)` where git cannot expand the path. `prefix` is the
/// context's answer: None with no context or where it is not known.
pub fn interpolate(
    machine: &Machine,
    path: &[u8],
    prefix: Option<&[u8]>,
) -> Result<Option<Vec<u8>>, PrefixUnknown> {
    if let Some(rest) = path.strip_prefix(RUNTIME_PREFIX) {
        let Some(found) = prefix else {
            return Err(PrefixUnknown);
        };
        if found == AS_WRITTEN {
            return Ok(Some(path.to_vec()));
        }
        if git_absolute(machine.flavour, rest) {
            return Ok(Some(rest.to_vec()));
        }
        let mut out = found.to_vec();
        out.push(b'/');
        out.extend_from_slice(rest);
        return Ok(Some(out));
    }
    if path.first() != Some(&b'~') {
        return Ok(Some(path.to_vec()));
    }
    let slash = path.iter().position(|&b| b == b'/').unwrap_or(path.len());
    let user = &path[1..slash];
    let found = if user.is_empty() {
        home(machine).map(|h| match machine.flavour {
            Flavour::Windows => h
                .iter()
                .map(|&b| if b == b'\\' { b'/' } else { b })
                .collect(),
            Flavour::Posix => h,
        })
    } else {
        user_home(machine, user)
    };
    Ok(found.map(|mut h| {
        h.extend_from_slice(&path[slash..]);
        h
    }))
}

/// `ntpath.splitroot` (Python 3.13): drive, root, rest.
fn nt_splitroot(p: &[u8]) -> (&[u8], &[u8], &[u8]) {
    let sep = |b: u8| b == b'/' || b == b'\\';
    if p.first().is_some_and(|&b| sep(b)) {
        if p.get(1).is_some_and(|&b| sep(b)) {
            let unc = p.len() >= 8
                && p[..8]
                    .iter()
                    .map(|&b| {
                        if b == b'/' {
                            b'\\'
                        } else {
                            b.to_ascii_uppercase()
                        }
                    })
                    .eq(b"\\\\?\\UNC\\".iter().copied());
            let start = if unc { 8 } else { 2 };
            let Some(index) = (start..p.len()).find(|&i| sep(p[i])) else {
                return (p, b"", b"");
            };
            let Some(index2) = (index + 1..p.len()).find(|&i| sep(p[i])) else {
                return (p, b"", b"");
            };
            return (&p[..index2], &p[index2..index2 + 1], &p[index2 + 1..]);
        }
        return (b"", &p[..1], &p[1..]);
    }
    // A drive is the first character and a colon (a character, not a
    // byte: `p[1:2]` of the `str`).
    let first = match p.first() {
        Some(&b) if b >= 0xC0 => {
            let n = if b >= 0xF0 {
                4
            } else if b >= 0xE0 {
                3
            } else {
                2
            };
            if p.len() >= n && std::str::from_utf8(&p[..n]).is_ok() {
                n
            } else {
                1
            }
        }
        _ => 1,
    };
    if p.len() > first && p[first] == b':' {
        let drive = first + 1;
        if p.get(drive).is_some_and(|&b| sep(b)) {
            return (&p[..drive], &p[drive..drive + 1], &p[drive + 1..]);
        }
        return (&p[..drive], b"", &p[drive..]);
    }
    (b"", b"", p)
}

/// `os.path.dirname` of the flavour.
pub fn dirname(flavour: Flavour, p: &[u8]) -> Vec<u8> {
    match flavour {
        Flavour::Posix => {
            let i = p.iter().rposition(|&b| b == b'/').map_or(0, |i| i + 1);
            let head = &p[..i];
            if !head.is_empty() && head.iter().any(|&b| b != b'/') {
                let end = head.iter().rposition(|&b| b != b'/').map_or(0, |i| i + 1);
                return head[..end].to_vec();
            }
            head.to_vec()
        }
        Flavour::Windows => {
            let (d, r, rest) = nt_splitroot(p);
            let i = rest
                .iter()
                .rposition(|&b| is_sep(flavour, b))
                .map_or(0, |i| i + 1);
            let head = &rest[..i];
            let end = head
                .iter()
                .rposition(|&b| !is_sep(flavour, b))
                .map_or(0, |i| i + 1);
            [d, r, &head[..end]].concat()
        }
    }
}

/// The bytes the OS opens for `path` where the process's working
/// directory is `cwd`: a relative path joined onto `cwd` as written, with
/// nothing normalised (the OS resolves `..` past links as it walks). An
/// empty path stays empty: the OS refuses it. The name a reader reports
/// stays `path`; only what it hands to the file system is this.
pub fn opened(flavour: Flavour, path: &[u8], cwd: &[u8]) -> Vec<u8> {
    let absolute = match flavour {
        Flavour::Posix => path.first() == Some(&b'/'),
        Flavour::Windows => {
            matches!(path.first(), Some(b'/' | b'\\')) || path.get(1) == Some(&b':')
        }
    };
    if path.is_empty() || absolute || cwd.is_empty() {
        return path.to_vec();
    }
    let mut out = cwd.to_vec();
    match flavour {
        Flavour::Posix if !out.ends_with(b"/") => out.push(b'/'),
        Flavour::Windows if !matches!(out.last(), Some(b'/' | b'\\')) => out.push(b'\\'),
        _ => {}
    }
    out.extend_from_slice(path);
    out
}

/// `os.path.join(a, b)` of the flavour.
pub fn join(flavour: Flavour, a: &[u8], b: &[u8]) -> Vec<u8> {
    match flavour {
        Flavour::Posix => {
            if b.first() == Some(&b'/') {
                return b.to_vec();
            }
            let mut out = a.to_vec();
            if !(out.is_empty() || out.ends_with(b"/")) {
                out.push(b'/');
            }
            out.extend_from_slice(b);
            out
        }
        Flavour::Windows => {
            let (rd, rr, rp) = nt_splitroot(a);
            let (mut drive, mut root, mut path) = (rd.to_vec(), rr.to_vec(), rp.to_vec());
            let (pd, pr, pp) = nt_splitroot(b);
            if !pr.is_empty() {
                if !pd.is_empty() || drive.is_empty() {
                    drive = pd.to_vec();
                }
                root = pr.to_vec();
                path = pp.to_vec();
            } else {
                let mut replaced = false;
                if !pd.is_empty() && pd != drive.as_slice() {
                    if !pd.eq_ignore_ascii_case(&drive) {
                        drive = pd.to_vec();
                        root = pr.to_vec();
                        path = pp.to_vec();
                        replaced = true;
                    } else {
                        drive = pd.to_vec();
                    }
                }
                if !replaced {
                    if path.last().is_some_and(|&c| !is_sep(flavour, c)) {
                        path.push(b'\\');
                    }
                    path.extend_from_slice(pp);
                }
            }
            if !path.is_empty()
                && root.is_empty()
                && drive
                    .last()
                    .is_some_and(|&c| c != b':' && !is_sep(flavour, c))
            {
                return [&drive[..], b"\\", &path[..]].concat();
            }
            [drive, root, path].concat()
        }
    }
}
