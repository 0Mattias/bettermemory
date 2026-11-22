//! `origin_gitfiles_attrs_posix.jsonl` has no Windows case: its generator
//! makes links, a FIFO and mode-000 entries and discovers repositories
//! through the owner's check, which the reference runs on POSIX only.
//! `golden_origin_gitfiles_attrs_posix.rs` runs on POSIX only; here the gap
//! is recorded, with the Windows flavour's stated declines: discovery and
//! `locate` find no repository there (realpath is not ported for it), so
//! `attribute_files_signature` and `gitdir_at_root` answer none, and
//! `gitattributes_signature` answers `NotPorted`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_attrs_fixture_has_no_case_here() {
    common::parity::record(
        "golden_origin_gitfiles_attrs_posix",
        "origin_gitfiles_attrs_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the fixture makes links, a FIFO and mode-000 entries and checks owners; \
             the Windows flavour declines discovery (realpath not ported) and \
             gitattributes_signature"
                .to_owned(),
        ],
    );
}
