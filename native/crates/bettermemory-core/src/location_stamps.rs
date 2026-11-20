//! `location_stamps.py` whole: a git directory's init stamps
//! (`_init_stamps`), whether its file system keeps its times in
//! FILETIME's 100 ns ticks (`in_ticks`, `kept_in_ticks`, `capture_ticks`,
//! with WSL's drvfs read from the mount table, `_drvfs`), and FILETIME's
//! arithmetic (`filetime_ns`, `basic_info_ns`).
//!
//! Every effect is an argument ([`Machine`]): the file system is the
//! crate's [`Fs`], the platform the reference reads from `sys.platform` is
//! a [`Platform`], and `_MOUNTINFO` is a path. The Windows call behind
//! `basic_info_ns` (`CreateFileW` and `GetFileInformationByHandleEx`,
//! loaded once by `_basic_info_calls` and `_load_basic_info`) is
//! [`Fs::basic_info`], which declines unless an implementation answers it.

pub use crate::fsdata::BasicInfo;
use crate::fsdata::{Flavour, Fs, PyErr, StatResult};
use crate::gitfile_rule::hostpath;

/// `_STAMPED`: the files a git directory's stamps are read from.
pub const STAMPED: [&str; 2] = ["description", "info/exclude"];
/// `_STAMPED_LINKED`: those of a linked worktree's git directory.
pub const STAMPED_LINKED: [&str; 1] = ["commondir"];
/// `_FILETIME_UNIX`: FILETIME's epoch in ticks before the Unix epoch.
pub const FILETIME_UNIX: i128 = 116_444_736_000_000_000;
/// `TICK_NS`: FILETIME's tick.
pub const TICK_NS: i128 = 100;
/// `SECOND_NS`.
pub const SECOND_NS: i128 = 1_000_000_000;
/// `_MOUNTINFO`.
pub const MOUNTINFO: &[u8] = b"/proc/self/mountinfo";
/// The size of each read of the mount table: `io.TextIOWrapper`'s chunk.
const CHUNK: usize = 8192;
/// `os.stat`'s ValueError for a str path holding a NUL.
pub const NUL_IN_PATH: &str = "stat: embedded null character in path";

/// `sys.platform` as the module reads it: `_WINDOWS` (`win32`), `_LINUX`
/// (`linux*`), and `os.major`/`os.minor`'s encoding of a device number,
/// which on Linux is the C library's that CPython was built against.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Platform {
    /// Linux under a CPython built against glibc, whose headers define
    /// `NODEV`.
    Linux,
    /// Linux under a CPython built against musl, whose headers do not.
    LinuxMusl,
    Darwin,
    Windows,
    /// Any other: neither flag; its device encoding is not ported.
    Other,
}

/// What the reference raises, or where the port declines.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StampError {
    /// `OverflowError` with its message (`os.major` of a device number
    /// `dev_t` cannot hold).
    Overflow(&'static str),
    /// `ValueError` with its message (`os.stat` of a path holding a NUL).
    Value(&'static str),
    /// `IndexError` (`git_key[2]` of a key shorter than three).
    Index,
    /// Not ported: what the machine does not give (an inode number or a
    /// change time not ported, FILE_BASIC_INFO declined, another
    /// platform's device encoding, an integer past `i128`).
    NotPorted(&'static str),
}

/// The machine the module reads: its file system, its platform and where
/// its mount table is.
pub struct Machine<'a, F: Fs + ?Sized> {
    pub fs: &'a F,
    pub platform: Platform,
    pub mountinfo: &'a [u8],
}

/// One init stamp: `(name, inode, mtime, ctime, size)`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Stamp {
    pub name: &'static str,
    pub ino: u64,
    pub mtime_ns: i128,
    pub ctime_ns: i128,
    pub size: u64,
}

/// `in_ticks(times)`: every time a whole tick and not every one a whole
/// second; False for none.
pub fn in_ticks(times: &[i128]) -> bool {
    if times.is_empty() || times.iter().any(|t| t % TICK_NS != 0) {
        return false;
    }
    times.iter().any(|t| t % SECOND_NS != 0)
}

/// `kept_in_ticks(times, device)`. `set(times)` changes no answer of
/// `in_ticks`, so the times are read as given.
pub fn kept_in_ticks<F: Fs + ?Sized>(
    machine: &Machine<'_, F>,
    times: &[i128],
    device: Option<i128>,
) -> Result<bool, StampError> {
    if machine.platform == Platform::Windows {
        return Ok(true);
    }
    if let Some(device) = device {
        if drvfs(machine, device)? {
            return Ok(true);
        }
    }
    Ok(in_ticks(times))
}

/// `stat_each(paths)`: each path's `os.stat`, None for no path or any
/// OSError or ValueError.
pub fn stat_each<F: Fs + ?Sized>(fs: &F, paths: &[Option<&[u8]>]) -> Vec<Option<StatResult>> {
    paths
        .iter()
        .map(|p| p.and_then(|p| fs.stat(p).ok()))
        .collect()
}

/// `capture_ticks(git_st, git_key, read, stamps)`. `git_key` is given as
/// whether each of its fields is None (`git_key[2] is None` is the only
/// read of it); a stamp as its mtime and ctime, the fields read.
pub fn capture_ticks<F: Fs + ?Sized>(
    machine: &Machine<'_, F>,
    git_st: Option<&StatResult>,
    git_key: Option<&[bool]>,
    read: &[Option<StatResult>],
    stamps: &[(i128, i128)],
) -> Result<Option<bool>, StampError> {
    let Some(git_st) = git_st else {
        return Ok(None);
    };
    let windows = machine.platform == Platform::Windows;
    let mut times = Vec::new();
    for s in read.iter().flatten() {
        if s.dev != git_st.dev {
            continue;
        }
        if git_st.dev.is_none() && !windows {
            return Err(StampError::NotPorted("st_dev"));
        }
        times.push(s.mtime_ns);
        match s.ctime_ns {
            Some(c) => times.push(c),
            None if windows => times.push(0),
            None => return Err(StampError::NotPorted("st_ctime_ns")),
        }
    }
    for &(m, c) in stamps {
        times.push(m);
        times.push(c);
    }
    let unborn = match git_key {
        None => false,
        Some(key) => *key.get(2).ok_or(StampError::Index)?,
    };
    if windows {
        return Ok(Some(true));
    }
    let dev = git_st.dev.ok_or(StampError::NotPorted("st_dev"))?;
    let device = unborn.then_some(i128::from(dev));
    kept_in_ticks(machine, &times, device).map(Some)
}

/// `_device_name(device)`: `major:minor` as `os.major` and `os.minor`
/// give them, `""` on Windows.
pub fn device_name(platform: Platform, device: i128) -> Result<String, StampError> {
    const NEGATIVE: &str = "can't convert negative int to unsigned";
    const TOO_BIG: &str = "int too big to convert";
    const NO_DEV_T: &str = "Python int too large to convert to C dev_t";
    // Whether CPython's C library defines `NODEV`, `(dev_t)-1`: macOS's
    // and glibc's do, musl's does not.
    let nodev = match platform {
        Platform::Windows => return Ok(String::new()),
        Platform::Other => return Err(StampError::NotPorted("device numbers")),
        Platform::Linux | Platform::Darwin => true,
        Platform::LinuxMusl => false,
    };
    // `_Py_Dev_Converter`: -1 is NODEV where it is defined, another
    // negative overflows, then `unsigned long long` and whether `dev_t`
    // holds it.
    let raw: u64 = if device == -1 && nodev {
        u64::MAX
    } else if device < 0 {
        return Err(StampError::Overflow(NEGATIVE));
    } else {
        u64::try_from(device).map_err(|_| StampError::Overflow(TOO_BIG))?
    };
    let (major, minor) = if platform == Platform::Darwin {
        // `dev_t` is `int32_t`: it holds the value where casting it
        // there and back gives it again.
        let low = raw as u32;
        if (low as i32 as i64) as u64 != raw {
            return Err(StampError::Overflow(NO_DEV_T));
        }
        ((low >> 24) & 0xff, low & 0x00ff_ffff)
    } else {
        // glibc's `gnu_dev_major` and `gnu_dev_minor`; musl's macros are
        // the same arithmetic. Each is an `unsigned int`.
        let major = ((raw >> 8) & 0xfff) | ((raw >> 32) & !0xfff & 0xffff_ffff);
        let minor = (raw & 0xff) | ((raw >> 12) & !0xff & 0xffff_ffff);
        (major as u32, minor as u32)
    };
    // `major_minor_conv`: a part equal to `(unsigned int)NODEV` is NODEV.
    let part = |v: u32| {
        if nodev && v == u32::MAX {
            "-1".to_owned()
        } else {
            v.to_string()
        }
    };
    Ok(format!("{}:{}", part(major), part(minor)))
}

/// `_drvfs(device)`: whether the mount table names `device` a WSL drvfs
/// mount. False off Linux or where the table cannot be read.
pub fn drvfs<F: Fs + ?Sized>(machine: &Machine<'_, F>, device: i128) -> Result<bool, StampError> {
    if !matches!(machine.platform, Platform::Linux | Platform::LinuxMusl) {
        return Ok(false);
    }
    let want = device_name(machine.platform, device)?;
    let fs = machine.fs;
    let Ok(mut file) = fs.open(machine.mountinfo) else {
        return Ok(false);
    };
    let mut pending: Vec<u8> = Vec::new();
    // Where the scan of `pending` resumes: bytes before it hold no break.
    let mut scanned = 0;
    loop {
        let Ok(chunk) = fs.read(&mut file, CHUNK) else {
            return Ok(false);
        };
        let end = chunk.is_empty();
        pending.extend_from_slice(&chunk);
        let mut start = 0;
        let mut i = scanned;
        while i < pending.len() {
            let cut = match pending[i] {
                b'\n' => Some(i + 1),
                b'\r' if i + 1 < pending.len() => Some(if pending[i + 1] == b'\n' {
                    i + 2
                } else {
                    i + 1
                }),
                b'\r' if end => Some(i + 1),
                _ => None,
            };
            match cut {
                Some(next) => {
                    if line_is_drvfs(&pending[start..next], &want) {
                        return Ok(true);
                    }
                    start = next;
                    i = next;
                }
                // A `\r` that may begin `\r\n`: wait for the next chunk.
                None if pending[i] == b'\r' => break,
                None => i += 1,
            }
        }
        pending.drain(..start);
        scanned = i - start;
        if end {
            return Ok(!pending.is_empty() && line_is_drvfs(&pending, &want));
        }
    }
}

/// One line of the mount table (UTF-8, errors replaced) read as `_drvfs`
/// reads it: split at `str.isspace`, the device in the third field, the
/// fields after the first `-` from the seventh on.
pub fn line_is_drvfs(line: &[u8], want: &str) -> bool {
    let text = String::from_utf8_lossy(line);
    let fields: Vec<&str> = text
        .split(|c: char| crate::unicode::is_space(u32::from(c)))
        .filter(|f| !f.is_empty())
        .collect();
    if fields.len() < 7 || fields[2] != want {
        return false;
    }
    let Some(dash) = fields[6..].iter().position(|f| *f == "-") else {
        return false;
    };
    let tail = &fields[6 + dash + 1..];
    let fstype = tail.first().copied().unwrap_or("");
    let source = tail.get(1).copied().unwrap_or("");
    let options = tail.get(2).copied().unwrap_or("");
    fstype == "drvfs"
        || (fstype == "9p" && aname_drvfs(options))
        || (fstype == "virtiofs" && source.starts_with("drvfs"))
}

/// `_DRVFS_ANAME.search(options)`, `(?:^|[,;])aname=drvfs(?:[,;]|$)`, by
/// hand: `options` holds no whitespace, so `$` is its end.
pub fn aname_drvfs(options: &str) -> bool {
    const NEEDLE: &str = "aname=drvfs";
    let b = options.as_bytes();
    let sep = |c: u8| c == b',' || c == b';';
    options.match_indices(NEEDLE).any(|(at, _)| {
        let after = at + NEEDLE.len();
        (at == 0 || sep(b[at - 1])) && (after == b.len() || sep(b[after]))
    })
}

/// `_init_stamps(git_dir, linked=linked)`, the path joined as `flavour`'s
/// `os.path.join`.
pub fn init_stamps<F: Fs + ?Sized>(
    machine: &Machine<'_, F>,
    flavour: Flavour,
    git_dir: &[u8],
    linked: bool,
) -> Result<Vec<Stamp>, StampError> {
    let names: &[&'static str] = if linked { &STAMPED_LINKED } else { &STAMPED };
    let mut out = Vec::new();
    for &name in names {
        let mut path = git_dir.to_vec();
        for part in name.split('/') {
            path = hostpath::join(flavour, &path, part.as_bytes());
        }
        let st = match machine.fs.stat(&path) {
            Ok(st) => st,
            Err(e) if e.class == PyErr::Value => return Err(StampError::Value(NUL_IN_PATH)),
            Err(_) => continue,
        };
        let changed = if machine.platform == Platform::Windows {
            match basic_info_ns(machine, &path)? {
                Some(info) => info[3],
                None => continue,
            }
        } else {
            st.ctime_ns.ok_or(StampError::NotPorted("st_ctime_ns"))?
        };
        out.push(Stamp {
            name,
            ino: st.ino.ok_or(StampError::NotPorted("st_ino"))?,
            mtime_ns: st.mtime_ns,
            ctime_ns: changed,
            size: st.size,
        });
    }
    Ok(out)
}

/// `filetime_ns(ticks)`: `(ticks - _FILETIME_UNIX) * 100`. None where the
/// answer is past `i128` (the reference's is a larger int); never for a
/// `c_int64`.
pub fn filetime_ns(ticks: i128) -> Option<i128> {
    ticks.checked_sub(FILETIME_UNIX)?.checked_mul(100)
}

/// `basic_info_ns(path)`: None off Windows or where a call fails.
pub fn basic_info_ns<F: Fs + ?Sized>(
    machine: &Machine<'_, F>,
    path: &[u8],
) -> Result<Option<[i128; 4]>, StampError> {
    if machine.platform != Platform::Windows {
        return Ok(None);
    }
    match machine.fs.basic_info(path) {
        BasicInfo::Declined => Err(StampError::NotPorted("FILE_BASIC_INFO")),
        BasicInfo::Failed => Ok(None),
        BasicInfo::Times(t) => Ok(Some(t.map(|v| (i128::from(v) - FILETIME_UNIX) * 100))),
    }
}
