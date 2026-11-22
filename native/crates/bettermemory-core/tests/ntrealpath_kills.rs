//! Cases a plausible wrong `ntpath.realpath`, `abspath`, `view_path` or
//! `probe` on Windows fails, each against 3.13.13's answer (read from the
//! generator's fake `nt` under that interpreter). Pure text: every
//! platform runs them.

mod ntfake;

use bettermemory_core::fsdata::realpath::{nt_abspath, nt_realpath, FinalPath, NtRaised};
use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFile, MemFs, Numbered, OsError, PyErr, Stat, StatResult,
};
use bettermemory_core::location_probe::{
    probe_windows, view_path_text, view_path_windows, Declined, Host, ProbeAnswer, Unbound,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use ntfake::Table;
use serde_json::json;

fn real(t: serde_json::Value, q: &str) -> Result<PyStr, NtRaised> {
    nt_realpath(&Table::of(&t), &cps(q), Ok(&cps("C:\\w")))
}

fn ok(s: &str) -> Result<PyStr, NtRaised> {
    Ok(cps(s))
}

#[test]
fn a_readlink_cycle_ends_where_it_meets_itself() {
    let t = json!({"f": {"C:\\a": 1921}, "r": {"C:\\a": "C:\\b", "C:\\b": "C:\\A"}});
    // `normcase` meets `C:\A` as `c:\a`: the walk stops there, on `C:\A`.
    assert_eq!(real(t, "C:\\a"), ok("C:\\A"));
}

#[test]
fn the_prefix_stays_unless_the_plain_path_answers_the_same() {
    let p = "\\\\?\\C:\\A";
    assert_eq!(real(json!({"f": {"C:\\a": p}}), "C:\\a"), ok(p));
    assert_eq!(
        real(json!({"f": {"C:\\a": p, "C:\\A": p}}), "C:\\a"),
        ok("C:\\A")
    );
    // The plain path raising the first call's code (0) is no match: 5.
    assert_eq!(real(json!({"f": {"C:\\a": p, "C:\\A": 5}}), "C:\\a"), ok(p));
    // A missing path keeps no prefix: the same code twice.
    assert_eq!(
        real(json!({"f": {"C:\\a": "\\\\?\\C:\\A"}}), "C:\\a\\b"),
        ok("C:\\A\\b")
    );
}

#[test]
fn only_a_bare_nul_is_the_device() {
    assert_eq!(real(json!({}), "NUL"), ok("\\\\.\\NUL"));
    assert_eq!(real(json!({}), "nul/"), ok("\\\\.\\NUL"));
    assert_eq!(real(json!({}), "C:\\nul"), ok("C:\\nul"));
    assert_eq!(real(json!({}), "nul.txt"), ok("C:\\w\\nul.txt"));
}

#[test]
fn the_working_directory_is_asked_for_an_absolute_path_too() {
    let nt = Table::of(&json!({}));
    assert_eq!(
        nt_realpath(&nt, &cps("C:\\abs"), Err(Some(2))),
        Err(NtRaised::Os(Some(2)))
    );
    // abspath asks it only for a relative path, and view_path falls to it.
    assert_eq!(
        nt_abspath(&nt, &cps("C:\\abs"), Err(Some(2))),
        Ok(cps("C:\\abs"))
    );
    assert_eq!(
        view_path_text(&nt, &cps("C:\\abs"), Err(Some(2))),
        Ok(cps("C:\\abs"))
    );
    assert_eq!(
        view_path_text(&nt, &cps("rel"), Err(Some(2))),
        Ok(cps("rel"))
    );
}

#[test]
fn an_unset_first_code_raises_unbound() {
    let nt = Table::of(&json!({"f": {"\\\\?\\C:\\w\\x\u{0}": null}}));
    let cwd = cps("\\\\?\\C:\\w");
    assert_eq!(
        nt_realpath(&nt, &cps("x\u{0}"), Ok(&cwd)),
        Err(NtRaised::Unbound)
    );
    assert_eq!(view_path_text(&nt, &cps("x\u{0}"), Ok(&cwd)), Err(Unbound));
}

#[test]
fn the_real_name_is_asked_only_after_its_codes() {
    let named = |code: i64| {
        let t = json!({"f": {"C:\\s\\x": code, "C:\\s": "C:\\S"}, "ff": {"C:\\s\\x": "X"}});
        real(t, "C:\\s\\x")
    };
    for code in [1, 5, 32, 50, 87, 1920, 1921] {
        assert_eq!(named(code), ok("C:\\S\\X"), "code {code}");
    }
    for code in [2, 3, 21, 53, 65, 67, 123, 161, 1005] {
        assert_eq!(named(code), ok("C:\\S\\x"), "code {code}");
    }
    assert_eq!(named(1450), Err(NtRaised::Os(Some(1450))));
}

#[test]
fn a_relative_target_is_joined_only_for_a_link() {
    let t = |links: &[&str]| json!({"f": {"C:\\d\\l": 1921}, "r": {"C:\\d\\l": "x"}, "l": links});
    assert_eq!(real(t(&["C:\\d\\l"]), "C:\\d\\l"), ok("C:\\d\\x"));
    assert_eq!(real(t(&[]), "C:\\d\\l"), ok("C:\\d\\l"));
}

#[test]
fn a_readlink_error_outside_its_codes_keeps_walking() {
    let t = json!({"f": {"C:\\a\\b": 1921, "C:\\a": "C:\\A"}, "r": {"C:\\a\\b": 1450}});
    assert_eq!(real(t, "C:\\a\\b"), ok("C:\\A\\b"));
}

#[test]
fn a_winerror_of_none_is_no_allowed_code() {
    struct NoCode;
    impl FinalPath for NoCode {
        fn final_path_name(
            &self,
            _: &[u32],
        ) -> Result<PyStr, bettermemory_core::fsdata::realpath::NtRaise> {
            Err(bettermemory_core::fsdata::realpath::NtRaise::Os(None))
        }
        fn full_path_name(&self, _: &[u32]) -> Option<PyStr> {
            None
        }
        fn readlink(
            &self,
            _: &[u32],
        ) -> Result<PyStr, bettermemory_core::fsdata::realpath::NtRaise> {
            Err(bettermemory_core::fsdata::realpath::NtRaise::Os(None))
        }
        fn find_first_file(
            &self,
            _: &[u32],
        ) -> Result<PyStr, bettermemory_core::fsdata::realpath::NtRaise> {
            Err(bettermemory_core::fsdata::realpath::NtRaise::Os(None))
        }
        fn is_link(&self, _: &[u32]) -> bool {
            false
        }
    }
    assert_eq!(
        nt_realpath(&NoCode, &cps("C:\\a"), Ok(&cps("C:\\w"))),
        Err(NtRaised::Os(None))
    );
}

#[test]
fn view_path_keeps_a_drive_root_and_strips_the_rest() {
    let nt = Table::of(&json!({}));
    let view = |q: &str| view_path_text(&nt, &cps(q), Ok(&cps("C:\\w")));
    assert_eq!(view("C:\\"), Ok(cps("C:\\")));
    assert_eq!(view("C:\\x\\\\"), Ok(cps("C:\\x")));
    assert_eq!(view("\\\\srv\\sh\\"), Ok(cps("\\\\srv\\sh\\")));
    assert_eq!(view("C:"), Ok(cps("C:\\w")));
}

#[test]
fn view_path_windows_spells_utf8_and_declines_other_bytes() {
    let nt = Table::of(&json!({"f": {"C:\\\u{e9}": "C:\\\u{c9}"}}));
    assert_eq!(
        view_path_windows(&nt, "C:\\\u{e9}".as_bytes(), Ok(b"C:\\w")),
        Ok("C:\\\u{c9}".as_bytes().to_vec())
    );
    assert_eq!(
        view_path_windows(&nt, b"C:\\\xff", Ok(b"C:\\w")),
        Err(Declined::Unknown)
    );
}

/// A file system whose `os.stat` answers `.1` for every path.
struct OneStat(MemFs, Result<StatResult, Numbered>);

impl Fs for OneStat {
    type File = MemFile;
    fn open(&self, path: &[u8]) -> Result<MemFile, PyErr> {
        self.0.open(path)
    }
    fn fstat(&self, file: &mut MemFile) -> Result<Stat, PyErr> {
        self.0.fstat(file)
    }
    fn read(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, PyErr> {
        self.0.read(file, max)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.0.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.0.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.0.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.0.readlink(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.stat_numbered(path).map_err(|raised| raised.error)
    }
    fn stat_numbered(&self, _: &[u8]) -> Result<StatResult, Numbered> {
        self.1.clone()
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.0.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.0.listdir(path)
    }
}

struct NoHost;

impl Host for NoHost {
    fn hostname(&self) -> Option<Vec<u8>> {
        Some(b"h".to_vec())
    }
    fn strerror(&self, _: &str) -> Option<String> {
        Some("gone text".into())
    }
    fn strerror_code(&self, _: i32) -> Option<String> {
        Some("gone text".into())
    }
}

#[test]
fn probe_windows_resolves_a_present_cwd_and_keys_a_gone_one_as_a_view() {
    let nt = Table::of(&json!({"f": {"C:\\p": "\\\\?\\C:\\P", "C:\\P": "\\\\?\\C:\\P"}}));
    let st = StatResult {
        kind: Kind::Directory,
        mode: None,
        ino: Some(9),
        dev: Some(7),
        size: 0,
        mtime_ns: 0,
        ctime_ns: None,
        flags: 0,
        file_attributes: 0,
        reparse_tag: None,
    };
    let got = probe_windows(
        &OneStat(MemFs::new(Flavour::Posix), Ok(st)),
        &NoHost,
        &nt,
        b"C:\\p",
        Ok(b"C:\\w"),
    )
    .unwrap();
    assert_eq!(got.cwd, b"C:\\P".to_vec());
    assert_eq!(
        got.answer,
        ProbeAnswer::Present {
            dev: 7,
            ino: 9,
            hostname: Some(b"h".to_vec())
        }
    );
    let mut error = OsError::new(PyErr::FileNotFound, "ENOENT");
    error.winerror = Some(2);
    let gone = Numbered {
        error,
        code: Some(2),
    };
    let got = probe_windows(
        &OneStat(MemFs::new(Flavour::Posix), Err(gone)),
        &NoHost,
        &nt,
        b"C:\\p\\",
        Ok(b"C:\\w"),
    )
    .unwrap();
    assert_eq!(got.cwd, b"C:\\P".to_vec());
    assert_eq!(
        got.answer,
        ProbeAnswer::Indeterminate {
            detail: "cwd gone: gone text".into()
        }
    );
}

#[test]
fn probe_windows_keeps_the_path_when_realpath_raises() {
    // `probe` catches realpath's OSError (here the working directory's
    // winerror 5) and keys the path as given.
    let nt = Table::of(&json!({}));
    let st = StatResult {
        kind: Kind::Directory,
        mode: None,
        ino: Some(3),
        dev: Some(4),
        size: 0,
        mtime_ns: 0,
        ctime_ns: None,
        flags: 0,
        file_attributes: 0,
        reparse_tag: None,
    };
    let got = probe_windows(
        &OneStat(MemFs::new(Flavour::Posix), Ok(st)),
        &NoHost,
        &nt,
        b"C:\\p",
        Err(Some(5)),
    )
    .unwrap();
    assert_eq!(got.cwd, b"C:\\p".to_vec());
    assert_eq!(
        got.answer,
        ProbeAnswer::Present {
            dev: 4,
            ino: 3,
            hostname: Some(b"h".to_vec())
        }
    );
}
