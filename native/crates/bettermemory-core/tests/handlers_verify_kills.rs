//! Cases a plausible wrong port of `handlers/verify.py`'s gates fails,
//! each with the reference's answer read from its source (CPython 3.13's
//! `posixpath.realpath`, `pathlib.PurePath.relative_to`).

mod common;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::handlers::verify::{
    check_verify_args, is_callers_tree, record_root, refuse_evidence_free_stamp,
    refuse_stale_stored_claims, refuse_unverifiable_stored_attestations, relative_to_root, within,
    AnchorFacts, Arg, DriftLists, Machine, ReadAt, Refused,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::{cps, to_string};

fn fs() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w", Node::Dir);
    fs.insert(b"/w/t", Node::Dir);
    fs.insert(b"/w/t/f", Node::File(Vec::new()));
    fs.insert(b"/w/l", Node::Link(b"/w/t".to_vec()));
    fs
}

fn m(fs: &MemFs) -> Machine<'_, MemFs> {
    Machine {
        fs,
        flavour: Flavour::Posix,
        cwd: Some(b"/w"),
    }
}

#[test]
fn a_lone_surrogate_in_a_path_raises_and_the_helper_falls_back() {
    let fs = fs();
    // `os.lstat` cannot encode U+D800: UnicodeEncodeError, a ValueError.
    let mut p = cps("/w/t/");
    p.push(0xD800);
    assert_eq!(
        relative_to_root(&m(&fs), &p, Some(&cps("/w"))),
        Ok(p.clone())
    );
    assert_eq!(within(&m(&fs), Some(&p), Some(&cps("/w"))), Ok(false));
    // U+DC80..U+DCFF encode to their byte (surrogateescape).
    let mut q = cps("/w/t/");
    q.push(0xDCFF);
    let mut want = cps("t/");
    want.push(0xDCFF);
    assert_eq!(relative_to_root(&m(&fs), &q, Some(&cps("/w"))), Ok(want));
}

#[test]
fn the_attestation_root_passed_on_is_the_original_spelling() {
    let fs = fs();
    let mut seen = None;
    let r = refuse_unverifiable_stored_attestations(
        &m(&fs),
        &[cps("a")],
        Some(&cps("/w/l/")),
        None,
        |_, how| {
            if let ReadAt::WorktreeRoot(r) = how {
                seen = r.map(<[u32]>::to_vec);
            }
            Vec::new()
        },
    );
    assert_eq!(r, Ok(()));
    assert_eq!(seen, Some(cps("/w/l/")));
}

#[test]
fn a_root_that_is_a_file_is_invisible() {
    let fs = fs();
    let r = refuse_stale_stored_claims(
        &m(&fs),
        &[cps("a")],
        Some(&cps("/w/t/f")),
        None,
        |s| s.to_vec(),
        |_: &PyStr, _| Some(cps("x")),
        |c| c.clone(),
        |_| false,
    );
    assert_eq!(r, Ok(()));
}

#[test]
fn contradicted_claims_win_over_unchecked_ones() {
    let fs = fs();
    let r = refuse_stale_stored_claims(
        &m(&fs),
        &[cps("a"), cps("b")],
        Some(&cps("/w/t")),
        None,
        |s| s.to_vec(),
        |c: &PyStr, _| Some(if c == &cps("a") { cps("?u") } else { cps("no") }),
        |c| c.clone(),
        |r| r.starts_with(&cps("?")),
    );
    let Err(Refused::ValueError(msg)) = r else {
        panic!("{r:?}")
    };
    assert!(to_string(&msg)
        .unwrap()
        .starts_with("cannot verify: 1 stored claim(s) no longer hold \u{2014} b: no."));
}

#[test]
fn a_non_convicting_anchor_skips_even_with_an_origin() {
    let fs = fs();
    let anchor = AnchorFacts {
        root: Some(cps("/w/t")),
        convicting: false,
    };
    let r = refuse_stale_stored_claims(
        &m(&fs),
        &[cps("a")],
        Some(&cps("/w/t")),
        Some(&anchor),
        |s| s.to_vec(),
        |_: &PyStr, _| Some(cps("x")),
        |c| c.clone(),
        |_| false,
    );
    assert_eq!(r, Ok(()));
}

#[test]
fn relative_to_compares_components_not_prefixes() {
    let fs = fs();
    let mm = m(&fs);
    assert_eq!(
        relative_to_root(&mm, &cps("/w/tt"), Some(&cps("/w/t"))),
        Ok(cps("/w/tt"))
    );
    assert_eq!(
        relative_to_root(&mm, &cps("/w/l"), Some(&cps("/w/t"))),
        Ok(cps("."))
    );
    assert_eq!(
        relative_to_root(&mm, &cps("t/f"), Some(&cps("/w/l"))),
        Ok(cps("f"))
    );
    assert_eq!(
        relative_to_root(&mm, &cps("/w/t"), Some(&cps("/w/t/f"))),
        Ok(cps("/w/t"))
    );
}

#[test]
fn within_needs_a_separator_after_the_root() {
    let fs = fs();
    let mm = m(&fs);
    assert_eq!(
        within(&mm, Some(&cps("/w/tt")), Some(&cps("/w/t"))),
        Ok(false)
    );
    assert_eq!(
        within(&mm, Some(&cps("/w/t/f")), Some(&cps("/w/l/"))),
        Ok(true)
    );
    assert_eq!(
        within(&mm, Some(&cps("/anything")), Some(&cps("/"))),
        Ok(true)
    );
    assert_eq!(
        is_callers_tree(&mm, Some(&cps("/w/l")), Some(&cps("/w/t/"))),
        Ok(true)
    );
}

#[test]
fn the_windows_flavour_declines() {
    let fs = fs();
    let mm = Machine {
        fs: &fs,
        flavour: Flavour::Windows,
        cwd: Some(b"C:\\"),
    };
    assert_eq!(
        within(&mm, Some(&cps("a")), Some(&cps("b"))),
        Err(Refused::NotPorted)
    );
    // No path: no resolution, no decline.
    assert_eq!(within(&mm, None, Some(&cps("b"))), Ok(false));
    assert_eq!(relative_to_root(&mm, &cps("a"), None), Ok(cps("a")));
}

#[test]
fn duplicates_count_and_the_ninth_is_elided() {
    let fs = fs();
    let checked: Vec<PyStr> = (0..9).map(|_| cps("/w/t/f")).collect();
    let r = refuse_evidence_free_stamp(
        &m(&fs),
        &cps("m"),
        &cps("b"),
        Some(&cps("/w/t")),
        false,
        false,
        false,
        None,
        |_| true,
        |_, _| DriftLists {
            checked,
            ..DriftLists::default()
        },
    );
    let Err(Refused::ValueError(msg)) = r else {
        panic!("{r:?}")
    };
    let msg = to_string(&msg).unwrap();
    assert!(msg.contains("cites 9 path(s)"), "{msg}");
    assert!(msg.contains("f, f, f, f, f, f, f, f, and 1 more."), "{msg}");
}

#[test]
fn an_origin_wins_over_the_anchoring() {
    let called = std::cell::Cell::new(false);
    let f = |_: &[u32]| {
        called.set(true);
        None
    };
    assert_eq!(
        record_root(Some(&cps("/o")), &cps("m"), Some(f)),
        Some(cps("/o"))
    );
    assert!(!called.get());
}

#[test]
fn caps_are_inclusive_and_lengths_count_code_points() {
    let note: PyStr = vec![0x1F600; 800];
    assert_eq!(
        check_verify_args(Arg::Given(&note), [Arg::Absent; 4]),
        Ok(())
    );
    let items: Vec<PyStr> = vec![cps("p"); 64];
    assert_eq!(
        check_verify_args(
            Arg::Absent,
            [Arg::Given(&items), Arg::Absent, Arg::Absent, Arg::Absent]
        ),
        Ok(())
    );
    let long: Vec<PyStr> = vec![vec![0xE9; 1025]];
    let e = check_verify_args(
        Arg::Absent,
        [Arg::Absent, Arg::Absent, Arg::Given(&long), Arg::Absent],
    )
    .unwrap_err();
    assert!(to_string(&e)
        .unwrap()
        .starts_with("verified_versions entry is 1025 chars"));
    // The type check of the note comes before the lists.
    let e = check_verify_args(Arg::WrongType, [Arg::WrongType; 4]).unwrap_err();
    assert_eq!(to_string(&e).unwrap(), "note must be a string if provided");
}

#[test]
fn a_low_surrogate_below_dc80_raises_rather_than_becoming_a_byte() {
    let fs = fs();
    // `os.fsencode` refuses U+DC00..U+DC7F (surrogateescape maps only
    // U+DC80..U+DCFF): ValueError, so `_within` answers False. Taken as
    // bytes, U+DC41 would be "A" and U+DC2F a separator.
    let mut p = cps("/w/");
    p.push(0xDC41);
    assert_eq!(within(&m(&fs), Some(&p), Some(&cps("/w"))), Ok(false));
    let mut q = cps("/w");
    q.push(0xDC2F);
    q.extend(cps("t"));
    assert_eq!(
        relative_to_root(&m(&fs), &q, Some(&cps("/w"))),
        Ok(q.clone())
    );
}

#[test]
fn an_absolute_path_resolves_without_a_working_directory() {
    let fs = fs();
    // `posixpath.realpath` of an absolute path never calls `os.getcwd`.
    let mm = Machine {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: None,
    };
    assert_eq!(
        is_callers_tree(&mm, Some(&cps("/w")), Some(&cps("/w"))),
        Ok(true)
    );
    assert_eq!(
        is_callers_tree(&mm, Some(&cps("/w/l")), Some(&cps("/w/t"))),
        Ok(true)
    );
    // A relative one does call it, and the raise reads as "not the tree".
    assert_eq!(
        is_callers_tree(&mm, Some(&cps("t")), Some(&cps("/w/t"))),
        Ok(false)
    );
}

#[test]
fn the_callers_tree_is_the_same_tree_not_a_nested_one() {
    let fs = fs();
    let mm = m(&fs);
    assert_eq!(
        is_callers_tree(&mm, Some(&cps("/w/t")), Some(&cps("/w"))),
        Ok(false)
    );
    assert_eq!(
        is_callers_tree(&mm, Some(&cps("/w")), Some(&cps("/w/t"))),
        Ok(false)
    );
    assert_eq!(
        is_callers_tree(&mm, Some(&cps("/w/l/")), Some(&cps("/w/t"))),
        Ok(true)
    );
}
