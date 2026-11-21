//! Inputs a plausible wrong `attestation_paths` answers otherwise than the
//! reference (each answer read off the reference over the scripted host of
//! `gen_attestation_paths`).

mod common;

use bettermemory_core::attestation_paths::{
    absolute_spelling, commonpath, compile_targets, interpret_path, may_be_literal, nt_splitdrive,
    placeholder_spelling, reference_path, target_path, FsHost, Mode, Raised,
};
use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::pyvalue::PyStr;
use common::attestation_paths::{cps, spell, spell_compiled, spell_opt, text, Scripted};

fn t(nt: bool, s: &str, root: Option<&str>) -> String {
    let root = root.map(cps);
    text(&spell(
        target_path(&Scripted { nt }, &cps(s), root.as_deref()),
        |v| spell_opt(&Some(v)),
    ))
}

#[test]
fn an_empty_root_holds_nothing() {
    // `commonpath((".", "x"))` is "", which is not ".".
    for nt in [false, true] {
        assert_eq!(t(nt, "a", Some("")), "!ValueError");
        assert_eq!(t(nt, "a", Some("r")), "Sr/a");
    }
}

#[test]
fn a_drive_root_is_ntpath_and_case_blind() {
    assert_eq!(t(false, "B", Some("C:\\R\\b\\..")), "SC:\\R\\B");
    // A UNC root without a share is a drive alone: the target has a root.
    assert_eq!(t(false, "x", Some("\\\\srv")), "!ValueError");
}

#[test]
fn the_drive_letter_is_any_unicode_letter() {
    assert!(absolute_spelling(&cps("\u{e9}:/a")));
    assert!(!absolute_spelling(&cps("1:/a")));
    assert!(!absolute_spelling(&cps("C:a")));
}

#[test]
fn a_repeated_input_keeps_its_first_place_and_the_legacy_list_repeats() {
    let host = Scripted { nt: false };
    let values = [cps("a"), cps("b"), cps("a")];
    let got = compile_targets(&host, &values, Some(&cps("/r")), Mode::Auto, false);
    assert_eq!(
        text(&spell_compiled(got)),
        "LLSa\u{1d}Sb\u{1d}Sa\u{1d}DSa=Sa\u{1d}Sb=Sb"
    );
}

#[test]
fn a_home_target_is_expanded_and_an_uncaught_raise_passes() {
    for (nt, home) in [(false, "/hm/u/a"), (true, "C:\\Hm\\u\\a")] {
        let host = Scripted { nt };
        let got = compile_targets(&host, &[cps("~/a")], None, Mode::Auto, false);
        assert_eq!(
            text(&spell_compiled(got)),
            format!("LLS~/a\u{1d}DS~/a=S{home}")
        );
        let got = compile_targets(&host, &[cps("~!/a")], None, Mode::Auto, false);
        assert_eq!(text(&spell_compiled(got)), "!KeyError");
    }
}

#[test]
fn an_impossible_windows_name_is_still_the_literal_when_absolute() {
    for nt in [false, true] {
        let got = interpret_path(&Scripted { nt }, &cps("C:\\a:b"), Mode::Auto, None);
        assert_eq!(text(&spell(got, |v| spell_opt(&v))), "SC:\\a:b");
    }
}

#[test]
fn a_placeholder_with_trailing_punctuation_is_refused() {
    let got = placeholder_spelling(
        &Scripted { nt: false },
        &cps("/path/to/file."),
        Mode::Auto,
        None,
    );
    assert_eq!(got, Ok(true));
    assert_eq!(reference_path(&cps("a/../..")), Ok(None));
}

#[test]
fn commonpath_and_splitdrive() {
    assert_eq!(commonpath(Flavour::Posix, &cps("/a"), &cps("a")), None);
    assert_eq!(
        commonpath(Flavour::Posix, &cps("/a/b"), &cps("/a/c")),
        Some(cps("/a"))
    );
    assert_eq!(
        commonpath(Flavour::Posix, &cps("a/./b"), &cps("a/b/c")),
        Some(cps("a/b"))
    );
    assert_eq!(
        commonpath(Flavour::Windows, &cps("C:\\a"), &cps("D:\\a")),
        None
    );
    assert_eq!(
        commonpath(Flavour::Windows, &cps("C:\\a"), &cps("C:a")),
        None
    );
    assert_eq!(
        commonpath(Flavour::Windows, &cps("C:/A/b"), &cps("c:\\a\\B\\x")),
        Some(cps("C:\\A\\b"))
    );
    assert_eq!(
        nt_splitdrive(&cps("\\\\s\\h\\x")),
        (cps("\\\\s\\h"), cps("\\x"))
    );
    assert_eq!(nt_splitdrive(&cps("C:x")), (cps("C:"), cps("x")));
}

#[test]
fn a_placeholder_whose_literal_raises_in_target_path_is_refused() {
    // The reference: placeholder_spelling(" /path/to,", mode="auto",
    // root=None) is True; the literal " /path/to," is relative, so
    // target_path raises ValueError, which the clause answers True.
    let host = Scripted { nt: false };
    let got = placeholder_spelling(&host, &cps(" /path/to,"), Mode::Auto, None);
    assert_eq!(got, Ok(true));
}

#[test]
fn the_placeholder_length_bound_holds_at_its_edge() {
    // `$HOME/path/to/` and x to 576 code points (_MAX_PATH_LENGTH + 64):
    // the reference answers True; one more code point answers False.
    let host = Scripted { nt: false };
    let mut raw = cps("$HOME/path/to/");
    raw.extend(std::iter::repeat_n(u32::from(b'x'), 576 - 14));
    assert_eq!(raw.len(), 576);
    let r = cps("/r");
    assert_eq!(
        placeholder_spelling(&host, &raw, Mode::Auto, Some(&r)),
        Ok(true)
    );
    raw.push(u32::from(b'x'));
    assert_eq!(
        placeholder_spelling(&host, &raw, Mode::Auto, Some(&r)),
        Ok(false)
    );
}

#[test]
fn a_lone_surrogate_under_the_windows_flavour_declines() {
    // On Windows `os.lstat` takes the wide string; a missing file raises
    // FileNotFoundError and the reference answers reference_path(raw).
    // The Windows flavour does not port the wide call, so the answer is a
    // decline, never the literal.
    let never = |_: &[u32]| -> Result<PyStr, Raised> { Err(Raised::NotPorted) };
    let host = FsHost {
        fs: &StdFs,
        nt: true,
        expanduser: &never,
    };
    let raw: PyStr = cps("C:/x").into_iter().chain([0xd800]).collect();
    assert_eq!(may_be_literal(&host, &raw), Err(Raised::NotPorted));
    assert_eq!(
        interpret_path(&host, &raw, Mode::Auto, None),
        Err(Raised::NotPorted)
    );
}
