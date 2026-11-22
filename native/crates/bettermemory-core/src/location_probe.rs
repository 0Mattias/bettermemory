//! The hook client's namespace probe (`_location_probe.py` 109-187):
//! `_path_state`, `view_path`, `_hostname` and `probe`. The reading of an
//! error as nothing at a path (`path_gone`, 60-106) is
//! [`crate::gitfile_rule::probe`], which this module reads with.
//!
//! Every read of the machine is an argument: the file system ([`Fs`]:
//! `os.stat`, and the `lstat` and `readlink` of `realpath`), the working
//! directory (`os.getcwd()`, `None` where it raises), and the host
//! ([`Host`]: `socket.gethostname()` and `os.strerror`). A path is bytes,
//! as `os.fsencode` spells the reference's `str`.
//!
//! [`view_path`] and [`probe`] over an [`Fs`] alone answer
//! [`Declined::NotPorted`] for the Windows flavour; [`view_path_windows`]
//! and [`probe_windows`] are the Windows flavour over the Windows calls
//! ([`FinalPath`]: `ntpath.realpath` and `abspath`, with `view_path`'s
//! drive rule). A path there is `os.fsencode`'s UTF-8; one that is not
//! UTF-8 declines ([`Declined::Unknown`]). [`path_state`] is the same on
//! both: the host's `os.stat` and `strerror`. Where
//! the reference would print text the port cannot know (`os.strerror`
//! off POSIX, a stat with no device or inode) the answer is
//! [`Declined::Unknown`], never a guess. The detail of an OSError is the
//! C library's text for its errno's number, any number
//! ([`Fs::stat_numbered`], `bettermemory_sys::strerror`), and for errno
//! 0 `str(exc)` as CPython's `os.stat` raises it; only an error with no
//! errno at all, which `os.stat` never raises, declines.

use crate::fsdata::realpath::{
    abspath, nt_abspath, nt_realpath, realpath, FinalPath, NtRaised, Unresolved,
};
use crate::fsdata::{Flavour, Fs, Numbered, PyErr, StatResult};
use crate::gitconfig::paths::repr_text;
use crate::gitfile_rule::hostpath::text;
use crate::gitfile_rule::probe::{path_gone, Raised};
use crate::pyvalue::PyStr;

/// `INDETERMINATE`: the state of a location nothing could be read for.
pub const INDETERMINATE: &str = "indeterminate";
/// `TOKEN_VERSION`: the location token's format version.
pub const TOKEN_VERSION: u32 = 1;
/// `str(ValueError)` of `os.stat` given a path holding a NUL.
pub const EMBEDDED_NUL: &str = "stat: embedded null character in path";

/// Why an answer is not given.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Declined {
    /// The Windows flavour, which is not ported.
    NotPorted,
    /// Text or a field the reference reads that the inputs do not carry.
    Unknown,
}

/// The host's own answers.
pub trait Host {
    /// `socket.gethostname()`: the name, or None where it raises OSError.
    fn hostname(&self) -> Option<Vec<u8>>;
    /// `os.strerror(errno)` for an errno by its name (an error made in
    /// memory carries the name alone); None where unknown.
    fn strerror(&self, errno: &str) -> Option<String>;
    /// `os.strerror(code)` for an errno by its number, any number; None
    /// where unknown. By default the text of the number's name, for a
    /// host that knows names only.
    fn strerror_code(&self, code: i32) -> Option<String> {
        self.strerror(bettermemory_sys::errno_name(code)?)
    }
}

/// The machine's host: `gethostname(2)` and the C library's `strerror`.
#[derive(Clone, Copy, Debug, Default)]
pub struct StdHost;

impl Host for StdHost {
    fn hostname(&self) -> Option<Vec<u8>> {
        bettermemory_sys::hostname().ok()
    }

    fn strerror(&self, errno: &str) -> Option<String> {
        // The number the platform gives the name, found by asking the
        // name of each small number: errno numbers differ by platform.
        let code = (1..512).find(|&c| bettermemory_sys::errno_name(c) == Some(errno))?;
        self.strerror_code(code)
    }

    fn strerror_code(&self, code: i32) -> Option<String> {
        bettermemory_sys::strerror(code)
    }
}

/// `_path_state(path)`'s answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PathState {
    /// `('present', stat, '')`.
    Present(StatResult),
    /// `('gone', None, detail)`: the OS answered that nothing is there.
    Gone(String),
    /// `('unreachable', None, detail)`: it could not say.
    Unreachable(String),
}

impl PathState {
    /// The state's name.
    pub fn name(&self) -> &'static str {
        match self {
            PathState::Present(_) => "present",
            PathState::Gone(_) => "gone",
            PathState::Unreachable(_) => "unreachable",
        }
    }
}

/// `_path_state(path)`: `os.stat(path)`; a ValueError is gone with its
/// text; an OSError is gone or unreachable by `path_gone`, its detail
/// `os.strerror(errno)`, or `str(exc)` where the errno is 0.
pub fn path_state<F: Fs + ?Sized, H: Host + ?Sized>(
    fs: &F,
    host: &H,
    path: &[u8],
) -> Result<PathState, Declined> {
    match fs.stat_numbered(path) {
        Ok(st) => Ok(PathState::Present(st)),
        Err(raised) if raised.error.class == PyErr::Value => {
            Ok(PathState::Gone(EMBEDDED_NUL.into()))
        }
        Err(raised) => {
            let reason = reason(host, raised, path)?;
            if path_gone(Raised::from(raised.error)) {
                Ok(PathState::Gone(reason))
            } else {
                Ok(PathState::Unreachable(reason))
            }
        }
    }
}

/// `os.strerror(exc.errno) if exc.errno else str(exc)`. A nonzero errno
/// is spelled by the host from its number, or from its name where the
/// error carries the name alone (one made in memory). Errno 0 is
/// `str(exc)` of what CPython's `os.stat` raises when the call left errno
/// 0, `OSError(0, "Error", path)` (`PyErr_SetFromErrnoWithFilenameObject`
/// writes "Error" for errno 0 on POSIX): `[Errno 0] Error: ` and the
/// path's `repr`. An OSError with no errno at all is one `os.stat` never
/// raises (CPython raises every failure with the errno it read), whose
/// `str(exc)` is its arguments', which no error here holds: declined.
fn reason<H: Host + ?Sized>(host: &H, raised: Numbered, path: &[u8]) -> Result<String, Declined> {
    let text = match (raised.code, raised.error.errno) {
        (Some(0), _) => {
            let shown = String::from_utf8_lossy(&repr_text(path)).into_owned();
            return Ok(format!("[Errno 0] Error: {shown}"));
        }
        (Some(code), _) => host.strerror_code(code),
        (None, Some(name)) => host.strerror(name),
        (None, None) => None,
    };
    text.ok_or(Declined::Unknown)
}

/// `os.path.realpath(path)` with `os.getcwd()` answering `cwd` (None:
/// it raises OSError, which the caller catches). `Err(None)` is an
/// OSError or ValueError the reference catches.
fn real(
    fs: &(impl Fs + ?Sized),
    path: &[u8],
    cwd: Option<&[u8]>,
) -> Result<Vec<u8>, Option<Declined>> {
    let absolute = path.starts_with(b"/");
    let cwd = match (absolute, cwd) {
        (true, _) => b"/".as_slice(),
        (false, Some(cwd)) => cwd,
        (false, None) => return Err(None),
    };
    match realpath(fs, Flavour::Posix, path, cwd) {
        Ok(real) => Ok(real),
        Err(Unresolved::NotPorted) => Err(Some(Declined::NotPorted)),
        Err(Unresolved::Raised(_)) => Err(None),
    }
}

/// `view_path(path)`: `realpath`, else `normpath(abspath(path))` (posix
/// `abspath` normalises), else the path as given; then without trailing
/// separators, unless nothing would be left. POSIX has no drive, so the
/// drive rule never applies.
pub fn view_path<F: Fs + ?Sized>(
    fs: &F,
    flavour: Flavour,
    path: &[u8],
    cwd: Option<&[u8]>,
) -> Result<Vec<u8>, Declined> {
    if flavour == Flavour::Windows {
        return Err(Declined::NotPorted);
    }
    let real = match real(fs, path, cwd) {
        Ok(real) => real,
        Err(Some(declined)) => return Err(declined),
        Err(None) => match (path.starts_with(b"/"), cwd) {
            (false, None) => path.to_vec(),
            (_, cwd) => abspath(Flavour::Posix, path, cwd.unwrap_or(b"/"))
                .map_err(|_| Declined::NotPorted)?,
        },
    };
    let end = real.iter().rposition(|&b| b != b'/').map_or(0, |i| i + 1);
    if end == 0 {
        return Ok(real);
    }
    Ok(real[..end].to_vec())
}

/// `_hostname()`: the host's name, None where it is empty or the call
/// raises.
pub fn hostname<H: Host + ?Sized>(host: &H) -> Option<Vec<u8>> {
    host.hostname().filter(|name| !name.is_empty())
}

/// `probe(cwd)`'s dict.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Probe {
    /// `cwd`: resolved where the path is present, as a view records it
    /// otherwise.
    pub cwd: Vec<u8>,
    pub answer: ProbeAnswer,
}

/// What follows `v`, `probe` and `cwd`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProbeAnswer {
    /// `state` (`INDETERMINATE`) and `detail`.
    Indeterminate { detail: String },
    /// `cwd_object` (`[str(dev), str(ino), None]`) and `hostname`.
    Present {
        dev: u64,
        ino: u64,
        hostname: Option<Vec<u8>>,
    },
}

/// `probe(cwd)`: one stat; where nothing is present, the cwd as a view
/// records it and the state's detail; else the cwd resolved, its device
/// and inode, and the host's name.
pub fn probe<F: Fs + ?Sized, H: Host + ?Sized>(
    fs: &F,
    host: &H,
    flavour: Flavour,
    path: &[u8],
    cwd: Option<&[u8]>,
) -> Result<Probe, Declined> {
    if flavour == Flavour::Windows {
        return Err(Declined::NotPorted);
    }
    let st = match path_state(fs, host, path)? {
        PathState::Present(st) => st,
        state => {
            let why = match &state {
                PathState::Gone(why) | PathState::Unreachable(why) => why.as_str(),
                PathState::Present(_) => "",
            };
            let detail = if why.is_empty() {
                format!("cwd {}", state.name())
            } else {
                format!("cwd {}: {why}", state.name())
            };
            return Ok(Probe {
                cwd: view_path(fs, flavour, path, cwd)?,
                answer: ProbeAnswer::Indeterminate { detail },
            });
        }
    };
    let resolved = match real(fs, path, cwd) {
        Ok(real) => real,
        Err(Some(declined)) => return Err(declined),
        Err(None) => path.to_vec(),
    };
    let (Some(dev), Some(ino)) = (st.dev, st.ino) else {
        return Err(Declined::Unknown);
    };
    Ok(Probe {
        cwd: resolved,
        answer: ProbeAnswer::Present {
            dev,
            ino,
            hostname: hostname(host),
        },
    })
}

/// What `view_path` raises where the reference lets an error through:
/// the UnboundLocalError of `ntpath.realpath` ([`NtRaised::Unbound`]).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Unbound;

fn decode(path: &[u8]) -> Result<PyStr, Declined> {
    let text = std::str::from_utf8(path).map_err(|_| Declined::Unknown)?;
    Ok(text.chars().map(u32::from).collect())
}

fn encode(text: &[u32]) -> Result<Vec<u8>, Declined> {
    let s: Option<String> = text.iter().map(|&u| char::from_u32(u)).collect();
    Ok(s.ok_or(Declined::Unknown)?.into_bytes())
}

/// `view_path(path)` with `_VIEW_PATHS` the Windows `ntpath`, over code
/// points: `realpath`, else `normpath(abspath(path))`, else the path; then
/// without trailing separators, except where that leaves a drive alone
/// (`normpath` of the resolved path then). `Err` is the UnboundLocalError
/// `view_path` does not catch.
pub fn view_path_text<P: FinalPath + ?Sized>(
    nt: &P,
    path: &[u32],
    cwd: Result<&[u32], Option<i64>>,
) -> Result<PyStr, Unbound> {
    let w = Flavour::Windows;
    let real = match nt_realpath(nt, path, cwd) {
        Ok(real) => real,
        Err(NtRaised::Unbound) => return Err(Unbound),
        Err(_) => match nt_abspath(nt, path, cwd) {
            Ok(abs) => text::normpath(w, &abs),
            Err(_) => path.to_vec(),
        },
    };
    let is_sep = |u: u32| u == u32::from(b'\\') || u == u32::from(b'/');
    let end = real.iter().rposition(|&u| !is_sep(u)).map_or(0, |i| i + 1);
    let stripped = &real[..end];
    let drive = |p: &[u32]| {
        let (d, r, rest) = crate::gitfile_rule::hostpath::splitroot(p);
        (d, [r, rest].concat())
    };
    if !drive(&real).0.is_empty() && drive(stripped).1.is_empty() {
        return Ok(text::normpath(w, &real));
    }
    if stripped.is_empty() {
        return Ok(real);
    }
    Ok(stripped.to_vec())
}

/// [`view_path_text`] over `os.fsencode`'s bytes; the UnboundLocalError
/// declines ([`Declined::Unknown`]).
pub fn view_path_windows<P: FinalPath + ?Sized>(
    nt: &P,
    path: &[u8],
    cwd: Result<&[u8], Option<i64>>,
) -> Result<Vec<u8>, Declined> {
    let cwd = match cwd {
        Ok(c) => Ok(decode(c)?),
        Err(e) => Err(e),
    };
    let view = view_path_text(nt, &decode(path)?, cwd.as_deref().map_err(|e| *e))
        .map_err(|Unbound| Declined::Unknown)?;
    encode(&view)
}

/// `probe(cwd)` on Windows: as [`probe`], `os.path.realpath` being
/// `ntpath`'s (its OSError or ValueError leaves the path as given; its
/// UnboundLocalError declines).
pub fn probe_windows<F: Fs + ?Sized, H: Host + ?Sized, P: FinalPath + ?Sized>(
    fs: &F,
    host: &H,
    nt: &P,
    path: &[u8],
    cwd: Result<&[u8], Option<i64>>,
) -> Result<Probe, Declined> {
    let st = match path_state(fs, host, path)? {
        PathState::Present(st) => st,
        state => {
            let (name, why) = match &state {
                PathState::Gone(why) => ("gone", why.as_str()),
                PathState::Unreachable(why) => ("unreachable", why.as_str()),
                PathState::Present(_) => ("present", ""),
            };
            let detail = if why.is_empty() {
                format!("cwd {name}")
            } else {
                format!("cwd {name}: {why}")
            };
            return Ok(Probe {
                cwd: view_path_windows(nt, path, cwd)?,
                answer: ProbeAnswer::Indeterminate { detail },
            });
        }
    };
    let ucwd = match cwd {
        Ok(c) => Ok(decode(c)?),
        Err(e) => Err(e),
    };
    let resolved = match nt_realpath(nt, &decode(path)?, ucwd.as_deref().map_err(|e| *e)) {
        Ok(real) => encode(&real)?,
        Err(NtRaised::Unbound) => return Err(Declined::Unknown),
        Err(_) => path.to_vec(),
    };
    let (Some(dev), Some(ino)) = (st.dev, st.ino) else {
        return Err(Declined::Unknown);
    };
    Ok(Probe {
        cwd: resolved,
        answer: ProbeAnswer::Present {
            dev,
            ino,
            hostname: hostname(host),
        },
    })
}
