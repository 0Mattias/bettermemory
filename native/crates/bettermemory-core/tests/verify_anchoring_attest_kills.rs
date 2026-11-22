//! Cases a plausible wrong port of the attestation checks fails, each
//! answered as the reference answers it (`verify_anchoring.py`).

mod common;

use bettermemory_core::fsdata::realpath::Unresolved;
use bettermemory_core::fsdata::Flavour;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::attest::{
    is_absolute_attestation, join_under, normalize_attestations, pure_text, worktree_root_is_live,
    RealFs,
};
use common::attestation_paths::{cps, text, Scripted};
use common::verify_anchoring_attest::{gate, keys, paths, Stub};

fn list(items: &[&str]) -> Vec<PyStr> {
    items.iter().map(|s| cps(s)).collect()
}

#[test]
fn the_drive_letter_is_any_alphabetic_code_point() {
    // `s[0].isalpha()`, not an ASCII letter test.
    assert!(is_absolute_attestation(&cps("\u{e9}:/x")));
    assert!(is_absolute_attestation(&cps("\u{4e2d}:\\x")));
    assert!(!is_absolute_attestation(&cps("1:/x")));
    assert!(!is_absolute_attestation(&cps("C:")));
    assert!(!is_absolute_attestation(&cps("$HOME")));
    assert!(is_absolute_attestation(&cps("~x")));
}

#[test]
fn a_join_spells_as_path_and_climbs_nothing() {
    // `str(Path("//r") / "a")` is "//r/a", which `_normalize_candidate`
    // refuses (two leading separators); "../x" escapes.
    assert_eq!(
        join_under(Flavour::Posix, &cps("//r"), &cps("a.py")).unwrap(),
        None
    );
    assert_eq!(
        join_under(Flavour::Posix, &cps("/r/./"), &cps("./a.py")).unwrap(),
        Some(cps("/r/a.py"))
    );
    assert_eq!(
        join_under(Flavour::Posix, &cps("/r"), &cps("a/../../x.py")).unwrap(),
        None
    );
    assert_eq!(text(&pure_text(Flavour::Posix, &cps("")).unwrap()), ".");
    assert_eq!(
        text(&pure_text(Flavour::Posix, &cps("///a//b/./")).unwrap()),
        "/a/b"
    );
}

#[test]
fn a_placeholder_is_refused_before_the_absolute_test() {
    let p = paths("P", &list(&["/path/to/ok"]));
    let (out, log) = gate(false, &p, None, None, false);
    assert_eq!(text(&out), "LS/path/to/ok");
    assert!(log.is_empty());
}

#[test]
fn reference_mode_strips_and_reports_the_stripped_spelling() {
    let p = paths("R", &list(&["  /gone/b  ", "   "]));
    let (out, _) = gate(false, &p, None, None, false);
    assert_eq!(text(&out), "LS/gone/b");
    let p = paths("P", &list(&["   "]));
    let (out, _) = gate(false, &p, Some(&cps("/r")), None, false);
    assert_eq!(text(&out), "L");
}

#[test]
fn a_relative_entry_without_a_root_or_a_join_is_skipped() {
    let p = paths("P", &list(&["gone.py", "src/../../x"]));
    let (out, log) = gate(false, &p, None, None, false);
    assert_eq!(text(&out), "L");
    assert!(log.is_empty());
    let (out, _) = gate(false, &p, Some(&cps("/r")), None, false);
    assert_eq!(text(&out), "LS/r/gone.py");
}

#[test]
fn a_stored_absolute_entry_refuses_only_under_a_convicting_rule() {
    let anchor = Stub {
        root: Some(cps("/r")),
        convicting: false,
        declared: None,
    };
    let p = paths("P", &list(&["/gone/a", "/hq/gone", "/un/gone"]));
    let (out, log) = gate(false, &p, None, Some(&anchor), true);
    assert_eq!(text(&out), "LS/H/hq/gone");
    // The standin's miss is not sparsity-checked: it never refuses.
    assert!(!log.iter().any(|e| text(e) == "s/gone/a"));
    let (out, _) = gate(false, &p, None, Some(&anchor), false);
    assert_eq!(text(&out), "LS/gone/a\u{1d}S/hq/gone\u{1d}S/un/gone");
}

#[test]
fn a_fresh_relative_entry_refuses_at_a_declared_root() {
    let p = paths("P", &list(&["gone.py", "sp/gone"]));
    let declared = Stub {
        root: Some(cps("/r")),
        convicting: false,
        declared: Some(cps("/r")),
    };
    let (out, _) = gate(false, &p, None, Some(&declared), false);
    assert_eq!(text(&out), "LS/r/gone.py");
    let (out, _) = gate(false, &p, None, Some(&declared), true);
    assert_eq!(text(&out), "L");
}

#[test]
fn the_claim_key_set_drops_the_empty_key_and_relative_entries() {
    let host = Scripted { nt: false };
    let p = paths("P", &list(&["/zz/a", "/a/k", "rel/k", "", "~/k"]));
    let got: Vec<String> = normalize_attestations(&host, &p, &keys)
        .unwrap()
        .iter()
        .map(|s| text(s))
        .collect();
    assert_eq!(got, vec!["/a/", "~/"]);
}

#[test]
fn a_non_ascii_drive_in_a_windows_join_replaces_the_root() {
    // CPython 3.13.13: `ntpath.join("C:\\r", "\u00e9:x.py")` reads "\u00e9:"
    // as a drive, so `Path` spells "\u00e9:x.py", which `_normalize_candidate`
    // refuses. Release-dependent, so pinned here and not in the fixture.
    assert_eq!(
        join_under(Flavour::Windows, &cps("C:\\r"), &cps("\u{e9}:x.py")).unwrap(),
        None
    );
}

#[test]
fn reference_mode_strips_every_python_space_at_the_end() {
    // `str.strip()` takes a tab, U+001F and U+3000 off the end as well.
    for tail in ["\t", "\u{1f}", "\u{3000}", " \u{85}"] {
        let p = paths("R", &list(&[&format!("/gone/b{tail}")]));
        let (out, _) = gate(false, &p, None, None, false);
        assert_eq!(text(&out), "LS/gone/b", "tail {tail:?}");
        // A placeholder is reported as spelled, so the strip itself shows.
        let p = paths("R", &list(&[&format!("/path/to/file{tail}")]));
        let (out, _) = gate(false, &p, None, None, false);
        assert_eq!(text(&out), "LS/path/to/file", "tail {tail:?}");
    }
}

#[test]
fn a_surrogate_root_answers_under_nt() {
    // `Path(worktree_root)` never raises: with no non-empty entry the
    // reference answers [] under nt whatever the root holds.
    let mut root = cps("$HOME/");
    root.push(0xdc80);
    for (kind, items) in [
        ("P", list(&[""])),
        ("P", list(&[])),
        ("R", list(&["  ", ""])),
    ] {
        let p = paths(kind, &items);
        let (out, _) = gate(true, &p, Some(&root), None, false);
        assert_eq!(text(&out), "L");
    }
    // An absolute or placeholder entry never joins the root, and a relative
    // one joins it under `PureWindowsPath`: each answers as the reference.
    for (item, want) in [
        ("/gone", cps("LS/gone")),
        ("C:/gone", cps("LSC:/gone")),
        ("/path/to/ok", cps("LS/path/to/ok")),
        ("rel.py", {
            let mut w = cps("LS$HOME\\");
            w.push(0xdc80);
            w.extend(cps("/rel.py"));
            w
        }),
    ] {
        let p = paths("P", &list(&[item]));
        let (out, _) = gate(true, &p, Some(&root), None, false);
        assert_eq!(out, want, "item {item:?}");
    }
}

#[test]
fn the_windows_spelling_keeps_a_lone_surrogate() {
    let mut t = cps("C:/a/");
    t.push(0xd800);
    t.extend(cps("//b"));
    let mut want = cps("C:\\a\\");
    want.push(0xd800);
    want.extend(cps("\\b"));
    assert_eq!(pure_text(Flavour::Windows, &t).unwrap(), want);
    // A text that already holds the stand-in code points still answers.
    let t = vec![0xf_0000, u32::from(b'/'), 0xdfff, 0xf_07ff];
    let got = pure_text(Flavour::Windows, &t).unwrap();
    assert_eq!(got, vec![0xf_0000, u32::from(b'\\'), 0xdfff, 0xf_07ff]);
}

/// Every path a directory, resolving to itself.
struct AllDirs;

impl RealFs for AllDirs {
    fn real(&self, path: &[u8], _: &[u8]) -> Result<Vec<u8>, Unresolved> {
        Ok(path.to_vec())
    }
    fn isdir(&self, _: &[u8]) -> bool {
        true
    }
    fn flavour(&self) -> Flavour {
        Flavour::Posix
    }
}

#[test]
fn a_root_expanduser_refuses_with_value_error_is_not_live() {
    // `except (OSError, ValueError): return False`; the scripted host's
    // `expanduser` raises ValueError for "~z".
    let host = Scripted { nt: false };
    assert!(!worktree_root_is_live(&host, &AllDirs, &cps("~zed/w")).unwrap());
    assert!(worktree_root_is_live(&host, &AllDirs, &cps("/w")).unwrap());
}

#[test]
fn a_root_holding_nul_is_not_live_even_where_every_path_is_a_folder() {
    // `os.path.isdir` catches the ValueError `os.stat` raises for a NUL.
    let host = Scripted { nt: false };
    assert!(!worktree_root_is_live(&host, &AllDirs, &cps("/w\u{0}")).unwrap());
}

#[test]
fn an_empty_entry_has_no_claim_key_in_any_mode() {
    let host = Scripted { nt: false };
    for kind in ["P", "R", "L"] {
        let p = paths(kind, &list(&["", "/a/k"]));
        let got = normalize_attestations(&host, &p, &keys).unwrap();
        assert_eq!(got.len(), 1, "kind {kind}");
    }
}

#[cfg(unix)]
#[test]
fn a_path_inside_a_link_loop_is_under_the_loop() {
    // CPython 3.13.13: `realpath` leaves a looping link unresolved, so
    // `@/loop/a` resolves to itself and is relative to `@/loop` (True).
    // 3.12's `Path.resolve` raises RuntimeError there (False), so this is
    // release-dependent and pinned here, not in the fixture.
    use bettermemory_core::fsdata::StdFs;
    use bettermemory_core::verify_anchoring::attest::{resolves_under, FlavouredFs};
    let base = std::env::temp_dir().join(format!("bm-vaat-loop-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).unwrap();
    let r = base.canonicalize().unwrap();
    std::os::unix::fs::symlink(r.join("loop"), r.join("loop")).unwrap();
    let at = |s: &str| cps(&format!("{}{s}", r.to_str().unwrap()));
    let host = Scripted { nt: false };
    let fs = FlavouredFs {
        fs: &StdFs,
        flavour: Flavour::Posix,
    };
    let under = |p: &str, root: &str| resolves_under(&host, &fs, b"/", &at(p), &at(root));
    let got = (
        under("/loop/a", "/loop"),
        under("/loop", "/loop"),
        under("/w/a", "/loop"),
    );
    let _ = std::fs::remove_dir_all(&base);
    assert!(got.0.unwrap());
    assert!(got.1.unwrap());
    assert!(!got.2.unwrap());
}
