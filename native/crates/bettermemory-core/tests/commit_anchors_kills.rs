//! Inputs on which a plausible wrong `commit_anchors` answers otherwise
//! than the reference (each answer read from CPython 3.13.13 on POSIX):
//! recorded roots of one length tried in another order, or ordered by
//! bytes; `absolute_spelling` read on the expanded path; a `..` path
//! inside `anchor.root` counted unmapped; no de-duplication; the `ntpath`
//! answer's backslashes kept; an empty root or origin read as a root;
//! an empty root read as `posixpath`, or passed to `relative_under` as no
//! root; the `..` and anchor-root tests read on the unexpanded path; a
//! failure of `os.getcwd()` swallowed.

use std::rc::Rc;

use bettermemory_core::commit_anchors::{
    expanduser, project_commit_anchors, relative_under, CommitAnchors, Failure, HostPaths, PathHost,
};
use bettermemory_core::fsdata::PyErr;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify_anchoring::anchor::{AnchorPaths, ClaimAnchor, TranslateFn};
use bettermemory_core::verify_candidates::scan::UserHome;

fn host() -> PathHost {
    PathHost {
        nt: false,
        cwd: Ok(cps("/w/cwd")),
    }
}

fn anchor(root: Option<&str>, rule: &str, roots: &[&str]) -> ClaimAnchor {
    let mut a = ClaimAnchor::new(root.map(str::to_string), rule);
    a.recorded_roots = roots.iter().map(|r| (*r).to_string()).collect();
    a
}

fn table(pairs: &[(&str, &str, &str)]) -> TranslateFn {
    let pairs: Vec<(String, String, String)> = pairs
        .iter()
        .map(|(p, t, r)| ((*p).into(), (*t).into(), (*r).into()))
        .collect();
    Rc::new(move |path: &str| {
        pairs
            .iter()
            .find(|(p, _, _)| p == path)
            .map_or((path.to_string(), "absolute".to_string()), |(_, t, r)| {
                (t.clone(), r.clone())
            })
    })
}

fn project_with(
    paths: &[&str],
    a: Option<&ClaimAnchor>,
    host: &PathHost,
    home: Option<&str>,
) -> Result<CommitAnchors, Failure> {
    let paths: Vec<PyStr> = paths.iter().map(|p| cps(p)).collect();
    let home = home.map(cps);
    let mut lookup = |_: &[u32]| UserHome::Unknown;
    project_commit_anchors(&paths, a, host, home.as_deref(), &mut lookup)
}

fn project(paths: &[&str], a: &ClaimAnchor) -> (Vec<String>, bool) {
    let got = project_with(paths, Some(a), &host(), Some("/hd/u")).expect("no failure");
    let texts = got
        .paths
        .iter()
        .map(|p| p.iter().map(|&c| char::from_u32(c).unwrap()).collect())
        .collect();
    (texts, got.unmapped)
}

#[test]
fn recorded_roots_of_one_length_keep_their_order() {
    let a = anchor(Some("/live"), "home_checkout", &["/r/a/b", "/r/a//"]);
    assert_eq!(project(&["/r/a/b/c"], &a), (vec!["c".into()], false));
    let a = anchor(Some("/live"), "home_checkout", &["/r/a//", "/r/a/b"]);
    assert_eq!(project(&["/r/a/b/c"], &a), (vec!["b/c".into()], false));
}

#[test]
fn recorded_roots_are_ordered_by_code_points_not_bytes() {
    // Seven code points each; nine and eight bytes.
    let (wide, narrow) = ("/\u{e9}/\u{fc}/..", "/\u{e9}/abcd");
    let a = anchor(Some("/live"), "home_checkout", &[wide, narrow]);
    assert_eq!(
        project(&["/\u{e9}/abcd/f"], &a),
        (vec!["abcd/f".into()], false)
    );
    let a = anchor(Some("/live"), "home_checkout", &[narrow, wide]);
    assert_eq!(project(&["/\u{e9}/abcd/f"], &a), (vec!["f".into()], false));
}

#[test]
fn the_longest_recorded_root_answers_first() {
    let a = anchor(Some("/live"), "home_checkout", &["/r", "/r/a"]);
    assert_eq!(project(&["/r/a/x"], &a), (vec!["x".into()], false));
}

#[test]
fn absolute_spelling_reads_the_path_as_given() {
    // `~/x` under HOME `rel` expands to `rel/x`, which is no absolute
    // spelling; the path as given is one, so the anchor is asked.
    let mut a = anchor(Some("/live"), "home_checkout", &[]);
    a.translate = Some(table(&[("rel/x", "/live/q", "checkout")]));
    let got = project_with(&["~/x"], Some(&a), &host(), Some("rel")).unwrap();
    assert_eq!(got.paths, vec![cps("q")]);
}

#[test]
fn a_parent_path_inside_the_anchor_root_is_not_unmapped() {
    let a = anchor(Some("/live"), "home_checkout", &["/live"]);
    assert_eq!(
        project(&["/live/a/../b"], &a),
        (vec!["/live/a/../b".into()], false)
    );
    let a = anchor(None, "no_live_checkout", &["/old"]);
    assert_eq!(
        project(&["/old/a/../b"], &a),
        (vec!["/old/a/../b".into()], true)
    );
    // An empty root is no root.
    let a = anchor(Some(""), "checkout", &["/old"]);
    assert_eq!(
        project(&["/old/a/../b"], &a),
        (vec!["/old/a/../b".into()], true)
    );
}

#[test]
fn backslashes_are_parts_only_off_a_posix_spelling() {
    // `/old/a\..\b` starts with `/`: no `..` part, so it is projected and
    // found under no root.
    let mut a = anchor(Some("/live"), "home_checkout", &["/old/x"]);
    a.origin_root = Some(cps("/old"));
    assert_eq!(project(&["/old/a\\..\\b"], &a), (vec![], true));
}

#[test]
fn equal_answers_are_kept_once_in_first_order() {
    let a = anchor(Some("/live"), "home_checkout", &["/old/a", "/old/b"]);
    let got = project(
        &["/old/b/y", "/old/a/x", "/old/a/x", "/old/b/x", "/old/b/y"],
        &a,
    );
    assert_eq!(got, (vec!["y".into(), "x".into()], false));
}

#[test]
fn no_anchor_and_a_global_record_answer_the_paths_as_given() {
    let paths = ["/old/x", "/old/x", "/old/x/"];
    let a = anchor(Some("/live"), "global_record", &["/old"]);
    let want: Vec<PyStr> = paths.iter().map(|p| cps(p)).collect();
    assert_eq!(
        project_with(&paths, Some(&a), &host(), None).unwrap().paths,
        want
    );
    assert_eq!(
        project_with(&paths, None, &host(), None).unwrap().paths,
        want
    );
}

#[test]
fn an_ntpath_answer_is_spelled_with_slashes() {
    let a = anchor(Some("/live"), "home_checkout", &["C:\\old"]);
    assert_eq!(project(&["C:\\old\\a\\b"], &a), (vec!["a/b".into()], false));
    let h = host();
    assert_eq!(
        relative_under(&cps("\\\\srv\\sh\\x\\y"), Some(&cps("\\\\srv\\sh")), &h),
        Ok(Some(cps("x/y")))
    );
    // A POSIX root keeps a backslash as data.
    assert_eq!(
        relative_under(&cps("/a/b\\c"), Some(&cps("/a")), &h),
        Ok(Some(cps("b\\c")))
    );
}

#[test]
fn an_empty_origin_root_is_no_root() {
    let mut a = anchor(Some("/live"), "home_checkout", &[]);
    a.origin_root = Some(Vec::new());
    a.translate = Some(table(&[]));
    assert_eq!(project(&["/o/x"], &a), (vec!["/o/x".into()], false));
    a.origin_root = Some(cps("/o"));
    assert_eq!(project(&["/o/x"], &a), (vec![], true));
}

#[test]
fn another_mount_is_none_and_a_lost_cwd_raises() {
    let h = host();
    assert_eq!(
        relative_under(&cps("\\\\a"), Some(&cps("\\")), &h),
        Ok(None)
    );
    let lost = PathHost {
        nt: false,
        cwd: Err(PyErr::FileNotFound),
    };
    assert_eq!(
        relative_under(&cps("a\\b"), Some(&cps("a")), &lost),
        Err(Failure::Os(PyErr::FileNotFound))
    );
    // Through the anchor's own `relative_under` too.
    let mut a = anchor(Some("rel"), "checkout", &[]);
    a.origin_root = Some(cps("rel"));
    a.translate = Some(table(&[]));
    assert_eq!(
        project_with(&["rel\\x"], Some(&a), &lost, None),
        Err(Failure::Os(PyErr::FileNotFound))
    );
    let paths = HostPaths::new(&lost);
    assert_eq!(paths.relative_under("rel\\x", Some("rel")), None);
    assert_eq!(paths.failure(), Some(Failure::Os(PyErr::FileNotFound)));
    assert_eq!(paths.failure(), None);
}

#[test]
fn expanduser_follows_posixpath() {
    let h = host();
    let mut lookup = |name: &[u32]| match name {
        [0x61] => UserHome::Found(cps("/hd/a//")),
        [0x72] => UserHome::Found(cps("/")),
        [0x7a] => UserHome::Refused,
        _ => UserHome::Unknown,
    };
    let mut ex = |p: &str, home: Option<&str>| {
        expanduser(&cps(p), &h, home.map(cps).as_deref(), &mut lookup)
    };
    assert_eq!(ex("~", Some("/")), Ok(cps("/")));
    assert_eq!(ex("~", Some("")), Ok(cps("/")));
    assert_eq!(ex("~/x", Some("/h/")), Ok(cps("/h/x")));
    assert_eq!(ex("~/x", None), Ok(cps("~/x")));
    assert_eq!(ex("~a/x", None), Ok(cps("/hd/a/x")));
    assert_eq!(ex("~r", None), Ok(cps("/")));
    assert_eq!(ex("~q/x", None), Ok(cps("~q/x")));
    assert_eq!(ex("~z/x", None), Err(Failure::Value));
    assert_eq!(ex("x~", Some("/h")), Ok(cps("x~")));
}

#[test]
fn the_windows_flavour_declines_only_where_windows_is_asked() {
    let nt = PathHost {
        nt: true,
        cwd: Ok(cps("C:\\w")),
    };
    assert_eq!(
        relative_under(&cps("C:\\a\\b"), Some(&cps("C:\\a")), &nt),
        Err(Failure::NotPorted)
    );
    assert_eq!(
        relative_under(&cps("D:\\a"), Some(&cps("C:\\a")), &nt),
        Ok(None)
    );
    assert_eq!(
        relative_under(&cps("/a/b"), Some(&cps("/a")), &nt),
        Ok(Some(cps("b")))
    );
    let a = anchor(Some("/live"), "home_checkout", &["/old"]);
    assert_eq!(
        project_with(&["~/x"], Some(&a), &nt, Some("C:\\u")),
        Err(Failure::NotPorted)
    );
    assert_eq!(
        project_with(&["/old/x"], Some(&a), &nt, None).map(|c| c.paths),
        Ok(vec![cps("x")])
    );
}

#[test]
fn a_surrogate_through_a_translating_anchor_declines() {
    let mut path = cps("/old/");
    path.push(0xDC80);
    let mut a = anchor(Some("/live"), "home_checkout", &[]);
    a.origin_root = Some(cps("/old"));
    let mut lookup = |_: &[u32]| UserHome::Unknown;
    // No translation: the spelling is read as code points.
    let got = project_commit_anchors(
        std::slice::from_ref(&path),
        Some(&a),
        &host(),
        None,
        &mut lookup,
    );
    assert_eq!(got.map(|c| c.unmapped), Ok(true));
    a.translate = Some(table(&[]));
    let got = project_commit_anchors(&[path], Some(&a), &host(), None, &mut lookup);
    assert_eq!(got, Err(Failure::NotPorted));
}

#[test]
fn an_empty_root_reads_as_ntpath() {
    // Only a root starting with `/` is `posixpath`: under `ntpath` the
    // backslash is a separator, so `.\` is `.`; `posixpath` would keep it.
    let h = host();
    assert_eq!(
        relative_under(&cps(".\\"), Some(&cps("")), &h),
        Ok(Some(cps(".")))
    );
    assert_eq!(relative_under(&cps(".\\.."), Some(&cps("")), &h), Ok(None));
}

#[test]
fn ntpath_answers_follow_the_reference_release() {
    // CPython 3.13.13 answers `b/C:`; 3.11 and 3.12 answer `C:`.
    assert_eq!(
        relative_under(&cps("C:\\a\\b\\C:"), Some(&cps("C:\\a")), &host()),
        Ok(Some(cps("b/C:")))
    );
    // A rooted path asks the working directory on 3.13 (3.11 and 3.12
    // read it as absolute and answer).
    let lost = PathHost {
        nt: false,
        cwd: Err(PyErr::FileNotFound),
    };
    assert_eq!(
        relative_under(&cps("\\w\\a"), Some(&cps("\\w\\a")), &lost),
        Err(Failure::Os(PyErr::FileNotFound))
    );
}

#[test]
fn the_parent_test_reads_the_expanded_path() {
    // `~/a\..\b` under HOME `/h` is `/h/a\..\b`, split on `/` alone: no
    // `..` part, so it is projected, found under no root, and unmapped.
    let mut a = anchor(Some("/live"), "home_checkout", &[]);
    a.origin_root = Some(cps("/h"));
    let got = project_with(&["~/a\\..\\b"], Some(&a), &host(), Some("/h")).unwrap();
    assert_eq!((got.paths, got.unmapped), (vec![], true));
}

#[test]
fn the_anchor_root_test_reads_the_expanded_path() {
    // `~/../x` under HOME `/live/u` is `/live/u/../x`, inside `/live`.
    let mut a = anchor(Some("/live"), "home_checkout", &[]);
    a.origin_root = Some(cps("/live/u"));
    let got = project_with(&["~/../x"], Some(&a), &host(), Some("/live/u")).unwrap();
    assert_eq!((got.paths, got.unmapped), (vec![cps("~/../x")], false));
}

#[test]
fn a_translated_target_under_an_empty_root_is_read_against_it() {
    // An empty `anchor.root` is passed as it is, not as no root: the
    // target `.` is relative to it.
    let mut a = anchor(Some(""), "checkout", &[]);
    a.origin_root = Some(cps("/o"));
    a.translate = Some(table(&[("/o/x", ".", "checkout")]));
    assert_eq!(project(&["/o/x"], &a), (vec![".".into()], false));
}
