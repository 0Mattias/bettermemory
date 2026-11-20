//! Cases a plausible wrong `location_probe` fails: gone against
//! unreachable told by `path_gone` (a Windows code over the class), the
//! detail's text, the trailing separators `view_path` strips and `probe`
//! keeps where `getcwd` raises, the host's empty name, and every decline.

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFile, MemFs, Node, Numbered, OsError, PyErr, Stat, StatResult,
};
use bettermemory_core::location_probe::{
    hostname, path_state, probe, view_path, Declined, Host, PathState, ProbeAnswer, StdHost,
};

/// A file system in memory whose `os.stat` raises `fault` (with its
/// errno's number where it carries one), or answers with no device or
/// inode where `blind`.
struct Faulty {
    fs: MemFs,
    fault: Option<Numbered>,
    blind: bool,
}

impl Fs for Faulty {
    type File = MemFile;
    fn open(&self, path: &[u8]) -> Result<MemFile, PyErr> {
        self.fs.open(path)
    }
    fn fstat(&self, file: &mut MemFile) -> Result<Stat, PyErr> {
        self.fs.fstat(file)
    }
    fn read(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, PyErr> {
        self.fs.read(file, max)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.fs.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.fs.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.fs.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.fs.readlink(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.stat_numbered(path).map_err(|raised| raised.error)
    }
    fn stat_numbered(&self, path: &[u8]) -> Result<StatResult, Numbered> {
        if let Some(fault) = self.fault {
            return Err(fault);
        }
        let mut st = self.fs.stat(path)?;
        if self.blind {
            st.ino = None;
        }
        Ok(st)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.fs.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.fs.listdir(path)
    }
}

struct Fixed(Option<Vec<u8>>);

impl Host for Fixed {
    fn hostname(&self) -> Option<Vec<u8>> {
        self.0.clone()
    }
    fn strerror(&self, errno: &str) -> Option<String> {
        match errno {
            "ENOENT" => Some("No such file or directory".into()),
            "EACCES" => Some("Permission denied".into()),
            "ELOOP" => Some("Too many levels of symbolic links".into()),
            _ => None,
        }
    }
}

fn host() -> Fixed {
    Fixed(Some(b"h".to_vec()))
}

fn world(fault: Option<OsError>) -> Faulty {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w", Node::Dir);
    fs.insert(b"/w/d", Node::Dir);
    fs.insert(b"/w/ld", Node::Link(b"d".to_vec()));
    Faulty {
        fs,
        fault: fault.map(Numbered::from),
        blind: false,
    }
}

/// [`world`] whose `os.stat` raises errno `code` by its number, the name
/// `bettermemory_sys::errno_name` gives it, and the class `class`.
fn numbered(class: PyErr, code: i32) -> Faulty {
    let mut fs = world(None);
    fs.fault = Some(Numbered {
        error: OsError {
            class,
            errno: bettermemory_sys::errno_name(code),
            winerror: None,
        },
        code: Some(code),
    });
    fs
}

fn detail(p: Result<bettermemory_core::location_probe::Probe, Declined>) -> String {
    match p.unwrap().answer {
        ProbeAnswer::Indeterminate { detail } => detail,
        other => panic!("present: {other:?}"),
    }
}

#[test]
fn a_permission_error_is_unreachable_and_a_loop_is_gone() {
    let fs = world(Some(OsError::new(PyErr::Permission, "EACCES")));
    let got = detail(probe(&fs, &host(), Flavour::Posix, b"/w/d", None));
    assert_eq!(got, "cwd unreachable: Permission denied");
    let fs = world(Some(OsError::new(PyErr::Os, "ELOOP")));
    let got = detail(probe(&fs, &host(), Flavour::Posix, b"/w/d", None));
    assert_eq!(got, "cwd gone: Too many levels of symbolic links");
}

#[test]
fn a_windows_code_outside_the_list_is_unreachable_whatever_the_class() {
    let fault = OsError {
        winerror: Some(53),
        ..OsError::new(PyErr::FileNotFound, "ENOENT")
    };
    let fs = world(Some(fault));
    assert_eq!(
        path_state(&fs, &host(), b"/w/d"),
        Ok(PathState::Unreachable("No such file or directory".into()))
    );
}

#[test]
fn a_nul_is_gone_with_the_interpreter_s_text() {
    let fs = world(Some(OsError::value()));
    let got = detail(probe(&fs, &host(), Flavour::Posix, b"/w/a\0b/", None));
    assert_eq!(got, "cwd gone: stat: embedded null character in path");
    let p = probe(&fs, &host(), Flavour::Posix, b"/w/a\0b//", None).unwrap();
    assert_eq!(p.cwd, b"/w/a\0b");
}

#[test]
fn view_path_strips_trailing_separators_but_never_the_root() {
    let fs = world(None);
    for (path, want) in [
        (&b"/"[..], &b"/"[..]),
        (b"//", b"/"),
        (b"///", b"/"),
        (b"/w/ld/", b"/w/d"),
        (b"/w/missing//", b"/w/missing"),
    ] {
        assert_eq!(
            view_path(&fs.fs, Flavour::Posix, path, Some(b"/w")).unwrap(),
            want,
            "{}",
            String::from_utf8_lossy(path)
        );
    }
}

#[test]
fn a_raising_getcwd_keeps_a_relative_path_as_given() {
    let fs = world(None);
    // view_path strips; probe's present branch keeps the separator.
    assert_eq!(view_path(&fs, Flavour::Posix, b"d/", None).unwrap(), b"d");
    let mut rooted = world(None);
    rooted.fs.insert(b"/d", Node::Dir);
    let p = probe(&rooted, &host(), Flavour::Posix, b"d/", None).unwrap();
    assert_eq!(p.cwd, b"d/");
    assert!(matches!(p.answer, ProbeAnswer::Present { .. }));
    // With a working directory it resolves.
    assert_eq!(
        view_path(&fs, Flavour::Posix, b"ld/", Some(b"/w")).unwrap(),
        b"/w/d"
    );
}

#[test]
fn the_host_s_empty_name_and_its_error_are_none() {
    assert_eq!(hostname(&Fixed(Some(Vec::new()))), None);
    assert_eq!(hostname(&Fixed(None)), None);
    assert_eq!(hostname(&Fixed(Some(b"x".to_vec()))), Some(b"x".to_vec()));
    let fs = world(None);
    let p = probe(
        &fs,
        &Fixed(Some(Vec::new())),
        Flavour::Posix,
        b"/w/ld",
        None,
    )
    .unwrap();
    assert_eq!(p.cwd, b"/w/d");
    match p.answer {
        ProbeAnswer::Present { hostname, .. } => assert_eq!(hostname, None),
        other => panic!("{other:?}"),
    }
}

#[test]
fn what_cannot_be_spelled_declines() {
    let fs = world(Some(OsError::new(PyErr::Os, "EIO")));
    assert_eq!(
        probe(&fs, &host(), Flavour::Posix, b"/w/d", None),
        Err(Declined::Unknown)
    );
    let fs = world(Some(OsError {
        class: PyErr::Os,
        errno: None,
        winerror: None,
    }));
    assert_eq!(
        probe(&fs, &host(), Flavour::Posix, b"/w/d", None),
        Err(Declined::Unknown)
    );
    let mut blind = world(None);
    blind.blind = true;
    assert_eq!(
        probe(&blind, &host(), Flavour::Posix, b"/w/d", None),
        Err(Declined::Unknown)
    );
    let fs = world(None);
    assert_eq!(
        probe(&fs, &host(), Flavour::Windows, b"/w/d", None),
        Err(Declined::NotPorted)
    );
    assert_eq!(
        view_path(&fs, Flavour::Windows, b"/w/d", None),
        Err(Declined::NotPorted)
    );
}

/// A host with text for every name, `answer`.
struct Every(&'static str);

impl Host for Every {
    fn hostname(&self) -> Option<Vec<u8>> {
        Some(b"h".to_vec())
    }
    fn strerror(&self, _errno: &str) -> Option<String> {
        Some(self.0.into())
    }
}

#[test]
fn an_error_with_no_errno_declines_even_where_every_name_has_text() {
    let fs = world(Some(OsError {
        class: PyErr::Os,
        errno: None,
        winerror: None,
    }));
    assert_eq!(
        probe(&fs, &Every("text"), Flavour::Posix, b"/w/d", None),
        Err(Declined::Unknown)
    );
}

#[test]
fn a_gone_errno_is_gone_whatever_the_class() {
    for name in ["ENOENT", "ENOTDIR", "ELOOP", "ENAMETOOLONG"] {
        let fs = world(Some(OsError::new(PyErr::Os, name)));
        assert_eq!(
            path_state(&fs, &Every("t"), b"/w/d"),
            Ok(PathState::Gone("t".into())),
            "{name}"
        );
    }
    for name in ["EIO", "EHOSTDOWN", "ENODEV", "ESTALE"] {
        let fs = world(Some(OsError::new(PyErr::Os, name)));
        assert_eq!(
            path_state(&fs, &Every("t"), b"/w/d"),
            Ok(PathState::Unreachable("t".into())),
            "{name}"
        );
    }
}

#[test]
fn an_empty_reason_leaves_the_state_alone() {
    let fs = world(Some(OsError::new(PyErr::FileNotFound, "ENOENT")));
    let got = detail(probe(&fs, &Every(""), Flavour::Posix, b"/w/d", None));
    assert_eq!(got, "cwd gone");
    let fs = world(Some(OsError::new(PyErr::Permission, "EACCES")));
    let got = detail(probe(&fs, &Every(""), Flavour::Posix, b"/w/d", None));
    assert_eq!(got, "cwd unreachable");
}

/// Errnos numbered above 63 on macOS or Linux. EHOSTDOWN's text both
/// share. ESTALE's does not: macOS says "Stale NFS file handle" and glibc
/// says "Stale file handle".
#[cfg(unix)]
#[test]
fn the_machine_spells_errnos_of_large_numbers() {
    let stale = StdHost.strerror("ESTALE");
    if cfg!(target_os = "macos") {
        assert_eq!(stale.as_deref(), Some("Stale NFS file handle"));
    } else if cfg!(target_os = "linux") {
        assert_eq!(stale.as_deref(), Some("Stale file handle"));
    } else {
        assert!(stale.is_some());
    }
    assert_eq!(
        StdHost.strerror("EHOSTDOWN").as_deref(),
        Some("Host is down")
    );
    let fs = world(Some(OsError::new(PyErr::Os, "EHOSTDOWN")));
    let got = detail(probe(&fs, &StdHost, Flavour::Posix, b"/w/d", None));
    assert_eq!(got, "cwd unreachable: Host is down");
    for name in ["ENODEV", "EOVERFLOW"] {
        let fs = world(Some(OsError::new(PyErr::Os, name)));
        let got = detail(probe(&fs, &StdHost, Flavour::Posix, b"/w/d", None));
        assert!(got.starts_with("cwd unreachable: "), "{name}: {got}");
    }
}

/// A link loop, walked in memory as on every platform: the stat is gone
/// with ELOOP's text, and the cwd is as `realpath` leaves a loop.
#[test]
fn a_link_loop_is_gone_and_keeps_its_spelling() {
    let mut fs = world(None);
    fs.fs.insert(b"/w/s", Node::Link(b"s".to_vec()));
    fs.fs.insert(b"/w/a", Node::Link(b"b".to_vec()));
    fs.fs.insert(b"/w/b", Node::Link(b"a".to_vec()));
    fs.fs.insert(b"/w/into", Node::Link(b"a".to_vec()));
    for (path, cwd) in [
        (&b"/w/s"[..], &b"/w/s"[..]),
        (b"/w/s/", b"/w/s"),
        (b"/w/s/x", b"/w/s/x"),
        (b"/w/a", b"/w/a"),
        (b"/w/b/x/", b"/w/b/x"),
        (b"/w/into", b"/w/a"),
    ] {
        let p = probe(&fs, &host(), Flavour::Posix, path, Some(b"/w")).unwrap();
        let shown = String::from_utf8_lossy(path);
        assert_eq!(p.cwd, cwd, "{shown}");
        assert_eq!(
            p.answer,
            ProbeAnswer::Indeterminate {
                detail: "cwd gone: Too many levels of symbolic links".into()
            },
            "{shown}"
        );
    }
}

/// Errno 0 leaves `os.strerror` unasked: the detail is `str(exc)` of the
/// OSError CPython's `os.stat` raises then, `OSError(0, "Error", path)`,
/// the path's `repr` in it (CPython 3.13.13's texts for these paths). A
/// host with text for every number is not asked.
#[test]
fn errno_zero_is_the_exception_s_text_with_the_path_s_repr() {
    let fs = numbered(PyErr::Os, 0);
    for (path, text) in [
        (&b"d"[..], "[Errno 0] Error: 'd'"),
        (b"it's", "[Errno 0] Error: \"it's\""),
        (b"a'b\"c", "[Errno 0] Error: 'a\\'b\"c'"),
        (b"caf\xc3\xa9\xff", "[Errno 0] Error: 'caf\u{e9}\\udcff'"),
        (b"t\tb\\", "[Errno 0] Error: 't\\tb\\\\'"),
        (b"\xe2\x80\x8b\x7f", "[Errno 0] Error: '\\u200b\\x7f'"),
        (b"/w/d", "[Errno 0] Error: '/w/d'"),
        (b"", "[Errno 0] Error: ''"),
    ] {
        assert_eq!(
            path_state(&fs, &Every("never"), path),
            Ok(PathState::Unreachable(text.into())),
            "{}",
            String::from_utf8_lossy(path)
        );
    }
    let got = detail(probe(&fs, &Every("never"), Flavour::Posix, b"d", None));
    assert_eq!(got, "cwd unreachable: [Errno 0] Error: 'd'");
}

/// A host that says which way it was asked.
struct Asked;

impl Host for Asked {
    fn hostname(&self) -> Option<Vec<u8>> {
        Some(b"h".to_vec())
    }
    fn strerror(&self, errno: &str) -> Option<String> {
        Some(format!("name {errno}"))
    }
    fn strerror_code(&self, code: i32) -> Option<String> {
        Some(format!("number {code}"))
    }
}

/// `os.strerror(exc.errno)` reads the number: an error that carries one
/// is spelled by it, its name aside; one made in memory with a name
/// alone is spelled by the name; the gone rule still reads the name and
/// class.
#[test]
fn a_number_is_spelled_by_number_and_a_name_alone_by_name() {
    let mut fs = world(None);
    fs.fault = Some(Numbered {
        error: OsError::new(PyErr::Os, "EIO"),
        code: Some(5),
    });
    assert_eq!(
        path_state(&fs, &Asked, b"/w/d"),
        Ok(PathState::Unreachable("number 5".into()))
    );
    let fs = world(Some(OsError::new(PyErr::Os, "EIO")));
    assert_eq!(
        path_state(&fs, &Asked, b"/w/d"),
        Ok(PathState::Unreachable("name EIO".into()))
    );
    let mut fs = world(None);
    fs.fault = Some(Numbered {
        error: OsError::new(PyErr::FileNotFound, "ENOENT"),
        code: Some(2),
    });
    assert_eq!(
        path_state(&fs, &Asked, b"/w/d"),
        Ok(PathState::Gone("number 2".into()))
    );
    for code in [-1, 1000, i32::MAX] {
        let fs = numbered(PyErr::Os, code);
        assert_eq!(
            path_state(&fs, &Asked, b"/w/d"),
            Ok(PathState::Unreachable(format!("number {code}"))),
            "{code}"
        );
    }
    let fs = numbered(PyErr::Os, 0);
    assert_eq!(
        path_state(&fs, &Asked, b"d"),
        Ok(PathState::Unreachable("[Errno 0] Error: 'd'".into()))
    );
}

/// The number POSIX gives `name` here.
#[cfg(unix)]
fn number_of(name: &str) -> i32 {
    (1..512)
        .find(|&c| bettermemory_sys::errno_name(c) == Some(name))
        .unwrap_or_else(|| panic!("no number for {name}"))
}

/// A host that knows names only spells a number that has a name, and
/// has no text for one without.
#[cfg(unix)]
#[test]
fn a_host_of_names_spells_a_named_number() {
    let fs = numbered(PyErr::Permission, number_of("EACCES"));
    assert_eq!(
        path_state(&fs, &host(), b"/w/d"),
        Ok(PathState::Unreachable("Permission denied".into()))
    );
    let fs = numbered(PyErr::Os, 1000);
    assert_eq!(path_state(&fs, &host(), b"/w/d"), Err(Declined::Unknown));
}

/// Every number through the machine, as CPython 3.13 answers with
/// `os.stat` raising it: an errno outside `errno_name`'s list (EDEADLK,
/// EAGAIN), numbers past the platform's list and below it, and a gone
/// errno by its number as `StdFs` raises it. The texts are the C
/// library's (macOS and glibc; musl's differ and are not pinned here).
#[cfg(unix)]
#[test]
fn the_machine_spells_every_number() {
    let texts: &[(i32, &str)] = if cfg!(target_os = "macos") {
        &[
            (11, "Resource deadlock avoided"),
            (35, "Resource temporarily unavailable"),
            (1000, "Unknown error: 1000"),
            (-1, "Unknown error: -1"),
        ]
    } else if cfg!(all(target_os = "linux", target_env = "gnu")) {
        &[
            (35, "Resource deadlock avoided"),
            (11, "Resource temporarily unavailable"),
            (41, "Unknown error 41"),
            (1000, "Unknown error 1000"),
            (-1, "Unknown error -1"),
        ]
    } else {
        &[]
    };
    for &(code, text) in texts {
        let mut fs = world(None);
        fs.fault = Some(Numbered::of_io(&std::io::Error::from_raw_os_error(code)));
        assert_eq!(
            path_state(&fs, &StdHost, b"/w/d"),
            Ok(PathState::Unreachable(text.into())),
            "{code}"
        );
    }
    let mut fs = world(None);
    let eloop = number_of("ELOOP");
    fs.fault = Some(Numbered::of_io(&std::io::Error::from_raw_os_error(eloop)));
    let got = detail(probe(&fs, &StdHost, Flavour::Posix, b"/w/d", None));
    assert_eq!(got, "cwd gone: Too many levels of symbolic links");
}

/// `StdFs`'s stat carries the number the OS raised, and none for the
/// ValueError of a NUL.
#[cfg(unix)]
#[test]
fn the_machine_s_stat_carries_the_errno_s_number() {
    use bettermemory_core::fsdata::StdFs;
    let missing = std::env::temp_dir().join(format!("bm-lprobe-none-{}", std::process::id()));
    let raised = StdFs
        .stat_numbered(missing.to_str().unwrap().as_bytes())
        .unwrap_err();
    assert_eq!(raised.code, Some(number_of("ENOENT")));
    assert_eq!(raised.error, OsError::new(PyErr::FileNotFound, "ENOENT"));
    let raised = StdFs.stat_numbered(b"/a\0b").unwrap_err();
    assert_eq!(raised, Numbered::from(OsError::value()));
    let got = path_state(&StdFs, &StdHost, missing.to_str().unwrap().as_bytes());
    assert_eq!(got, Ok(PathState::Gone("No such file or directory".into())));
}

/// The machine's text for a number past every platform's list: the C
/// library's own on POSIX (macOS, glibc and musl each have one), and a
/// decline elsewhere, where `os.strerror` is not ported.
#[test]
fn the_machine_spells_a_number_on_posix_and_declines_elsewhere() {
    let fs = numbered(PyErr::Os, 1000);
    let got = path_state(&fs, &StdHost, b"/w/d");
    if cfg!(unix) {
        let Ok(PathState::Unreachable(text)) = got else {
            panic!("{got:?}");
        };
        assert!(
            [
                "Unknown error: 1000",
                "Unknown error 1000",
                "No error information"
            ]
            .contains(&text.as_str()),
            "{text}"
        );
    } else {
        assert_eq!(got, Err(Declined::Unknown));
    }
}
