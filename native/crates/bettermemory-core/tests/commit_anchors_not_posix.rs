//! `commit_anchors_posix.jsonl` has no Windows case: its rows reach
//! `ntpath.relpath`, which asks `nt._getfullpathname` on Windows, or
//! expand a `~` path, which `ntpath.expanduser` reads from `USERPROFILE`.
//! The port declines both under the Windows flavour (`Failure::NotPorted`),
//! the crate's stated Windows gap. `golden_commit_anchors_posix.rs` runs on
//! POSIX only; here the gap is recorded and the decline checked.

#![cfg(not(unix))]

mod common;

use bettermemory_core::commit_anchors::{expanduser, relative_under, Failure, PathHost};
use bettermemory_core::unicode::cps;
use bettermemory_core::verify_candidates::scan::UserHome;

#[test]
fn the_posix_commit_anchors_fixture_has_no_case_here() {
    common::parity::record(
        "golden_commit_anchors_posix",
        "commit_anchors_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["ntpath.relpath and ntpath.expanduser ask Windows; the port declines".to_owned()],
    );
}

#[test]
fn the_windows_flavour_declines_where_the_reference_asks_windows() {
    let host = PathHost::here(Ok(cps("C:\\w")));
    assert!(host.nt);
    let got = relative_under(&cps("C:\\a\\b"), Some(&cps("C:\\a")), &host);
    assert_eq!(got, Err(Failure::NotPorted));
    let got = relative_under(&cps("/a/b"), Some(&cps("/a")), &host);
    assert_eq!(got, Ok(Some(cps("b"))));
    let mut lookup = |_: &[u32]| UserHome::Unknown;
    assert_eq!(
        expanduser(&cps("~/x"), &host, Some(&cps("C:\\u")), &mut lookup),
        Err(Failure::NotPorted)
    );
    assert_eq!(
        expanduser(&cps("x/~"), &host, None, &mut lookup),
        Ok(cps("x/~"))
    );
}
