//! Cases a plausible wrong port of `membership_records` unit 1 fails,
//! each answer the reference's (CPython 3.13.13, checked by running it).

use bettermemory_core::capture::location::ObjectKey;
use bettermemory_core::fsdata::{Flavour, PyErr};
use bettermemory_core::gitfile_rule::hostpath::text::{self, Process, Raised};
use bettermemory_core::membership_records::{self as mr, paths};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

struct Cwd(Option<&'static str>);

impl Process for Cwd {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        self.0.map(cps).ok_or(PyErr::FileNotFound)
    }
}

fn rebase(
    path: &str,
    root: &str,
    target: &str,
    cwd: Option<&'static str>,
) -> Result<PyStr, Raised> {
    paths::rebase(&cps(path), &cps(root), &cps(target), &Cwd(cwd))
}

#[test]
fn the_legacy_rule_reads_two_words_exactly() {
    assert!(mr::legacy_asks(&cps("ambiguous"), false, false));
    assert!(mr::legacy_asks(&cps("none"), true, true));
    assert!(!mr::legacy_asks(&cps("none"), true, false));
    assert!(!mr::legacy_asks(&cps("none"), false, true));
    assert!(!mr::legacy_asks(&cps("Ambiguous"), true, true));
    assert!(!mr::legacy_asks(&cps("none "), true, true));
    assert!(!mr::legacy_asks(&cps("\u{212a}one"), true, true));
}

#[test]
fn the_number_ignores_the_birth_time_and_a_missing_key() {
    let k = |d: &str, i: &str, b: Option<&str>| ObjectKey {
        device: cps(d),
        inode: cps(i),
        birth: b.map(cps),
    };
    assert!(mr::same_number(
        Some(&k("1", "2", Some("9"))),
        Some(&k("1", "2", None))
    ));
    assert!(!mr::same_number(
        Some(&k("1", "2", None)),
        Some(&k("2", "2", None))
    ));
    assert!(!mr::same_number(None, None));
}

#[test]
fn the_flavour_is_an_ascii_letter_and_a_colon_or_a_backslash() {
    assert_eq!(paths::flavour(&cps("1:a")), Flavour::Posix);
    assert_eq!(paths::flavour(&cps("\u{3a3}:a")), Flavour::Posix);
    assert_eq!(paths::flavour(&cps("z:")), Flavour::Windows);
    assert_eq!(paths::flavour(&cps("/a\\b")), Flavour::Windows);
}

#[test]
fn below_trims_every_trailing_separator_of_the_root() {
    assert!(paths::dir_below(Flavour::Posix, &cps("/a/b"), &cps("/a//")));
    assert!(paths::dir_below(
        Flavour::Windows,
        &cps("c:\\a\\b"),
        &cps("c:\\a\\\\")
    ));
    assert!(!paths::dir_below(Flavour::Posix, &cps("/a"), &cps("/a")));
    assert!(!paths::dir_below(Flavour::Posix, &cps("/ab"), &cps("/a")));
    assert!(paths::dir_below(Flavour::Posix, &cps("/a"), &cps("")));
}

#[test]
fn the_key_folds_as_str_lower_with_its_final_sigma() {
    let key = |h, p: &str| paths::dir_key(h, &cps(p));
    assert_eq!(
        key(Flavour::Windows, "D:/\u{391}\u{3a3}"),
        cps("d:\\\u{3b1}\u{3c2}")
    );
    assert_eq!(key(Flavour::Windows, "D:\\\u{130}"), cps("d:\\i\u{307}"));
    assert_eq!(key(Flavour::Posix, "//a/../b"), cps("//b"));
    assert_eq!(key(Flavour::Posix, "///a/../b"), cps("/b"));
}

#[test]
fn rebase_reads_the_root_and_writes_the_target() {
    assert_eq!(
        rebase("c:\\V\\a", "C:\\v", "/t", Some("/w")),
        Ok(cps("/t/a"))
    );
    assert_eq!(
        rebase("/v/a\\b", "/v", "C:\\t", Some("/w")),
        Ok(cps("C:\\t\\a\\b"))
    );
    assert_eq!(
        rebase("D:\\v\\a", "C:\\v", "/t", Some("/w")),
        Err(Raised::Value(cps(
            "path is on mount 'D:', start on mount 'C:'"
        )))
    );
    assert_eq!(
        rebase("", "/v", "/t", Some("/w")),
        Err(Raised::Value(cps("no path specified")))
    );
    assert_eq!(
        rebase("v/a", "v", "t", None),
        Err(Raised::Os(PyErr::FileNotFound))
    );
    // An absolute pair never asks for the working directory.
    assert_eq!(rebase("/v/a", "/v", "/t", None), Ok(cps("/t/a")));
}

#[test]
fn joins_and_names_keep_the_spelling() {
    let parts = [cps("c:x")];
    let refs: Vec<&[u32]> = parts.iter().map(Vec::as_slice).collect();
    assert_eq!(paths::path_under(&cps("C:\\a"), &refs), cps("c:\\a\\x"));
    assert_eq!(paths::basename(&cps("/a\\b")), cps("a\\b"));
    assert_eq!(paths::basename(&cps("a\\b\\")), cps("b"));
    assert!(!paths::same_path(&cps("/a\\"), &cps("/a")));
}

#[test]
fn the_pointer_reads_worktrees_as_spelled() {
    let c = |h, kind: &str, g: &str| paths::common_by_pointer(h, Some(&cps(kind)), Some(&cps(g)));
    assert_eq!(c(Flavour::Windows, "linked", "/r/.git/Worktrees/w"), None);
    assert_eq!(
        c(Flavour::Posix, "linked", "/r/.git/worktrees/w//"),
        Some(cps("/r/.git"))
    );
    assert_eq!(
        paths::common_by_pointer(Flavour::Posix, None, Some(&cps("/r"))),
        None
    );
    assert_eq!(
        paths::common_by_pointer(Flavour::Posix, Some(&cps("bare")), Some(&[])),
        None
    );
}

/// A Windows host: `nt._getfullpathname` answers `full` for the one path
/// `asked` and raises for every other; `os.getcwd` answers `cwd` or raises.
struct Windows {
    asked: &'static str,
    full: &'static str,
    cwd: Option<&'static str>,
}

impl Process for Windows {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        self.cwd.map(cps).ok_or(PyErr::FileNotFound)
    }

    fn windows(&self) -> bool {
        true
    }

    fn full_path_name(&self, path: &[u32]) -> Option<PyStr> {
        (path == cps(self.asked).as_slice()).then(|| cps(self.full))
    }
}

/// Answers checked against CPython 3.13.13's `ntpath` loaded with a stub
/// `nt` module that answers `_getfullpathname` as the host here does.
#[test]
fn abspath_on_a_windows_host_falls_back_as_the_reference_does() {
    let abspath = |path: &str, host: &Windows| text::abspath(Flavour::Windows, &cps(path), host);
    let none = Windows {
        asked: "",
        full: "",
        cwd: Some("C:\\w"),
    };
    assert_eq!(abspath("C:\\a\\..\\b", &none), Ok(cps("C:\\b")));
    assert_eq!(abspath("C:x", &none), Ok(cps("C:\\x")));
    assert_eq!(abspath("x\\..\\y", &none), Ok(cps("C:\\w\\y")));
    let drive = Windows {
        asked: "C:",
        full: "C:\\w",
        cwd: None,
    };
    assert_eq!(abspath("C:x", &drive), Ok(cps("C:\\w\\x")));
    let whole = Windows {
        asked: "C:\\q",
        full: "D:\\full",
        cwd: None,
    };
    assert_eq!(abspath("C:\\q", &whole), Ok(cps("D:\\full")));
    assert_eq!(abspath("x", &whole), Err(Raised::Os(PyErr::FileNotFound)));
}

/// `relpath` reads each path through `abspath` unnormalised: the root `/`
/// is asked of `_getfullpathname` as `/`, not as `\`.
#[test]
fn relpath_on_a_windows_host_asks_each_path_as_given() {
    let root = Windows {
        asked: "\\",
        full: "C:\\w",
        cwd: None,
    };
    assert_eq!(
        text::relpath(Flavour::Windows, &cps("/x"), &cps("\\"), &root),
        Err(Raised::Value(cps(
            "path is on mount '', start on mount 'C:'"
        )))
    );
}

#[test]
fn relpath_passes_on_what_getcwd_raised() {
    assert_eq!(
        text::relpath(Flavour::Posix, &cps("a"), &cps("/b"), &Cwd(None)),
        Err(Raised::Os(PyErr::FileNotFound))
    );
    assert_eq!(
        text::relpath(Flavour::Windows, &cps("C:\\a"), &cps("b"), &Cwd(None)),
        Err(Raised::Os(PyErr::FileNotFound))
    );
}

#[test]
fn normcase_swaps_each_slash_and_folds_on_nt_only() {
    assert_eq!(text::normcase(Flavour::Windows, &cps("A/B")), cps("a\\b"));
    assert_eq!(text::normcase(Flavour::Posix, &cps("A/B")), cps("A/B"));
}
