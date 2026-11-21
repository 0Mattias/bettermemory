//! Hostile input for `project_file`: long names and ids through
//! `toml_string`, `render` and the error's line; time linear in the text.

mod timing;

use bettermemory_core::project_file::{
    render, toml_string, ErrorReason, ProjectFile, ProjectFileError,
};
use bettermemory_core::pyvalue::PyStr;

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

#[test]
fn long_texts_grow_linearly() {
    let small: Vec<u32> = (0..20_000u32).map(|i| i % 0x3000).collect();
    let large: Vec<u32> = (0..200_000u32).map(|i| i % 0x3000).collect();
    timing::grows_within(
        "toml_string",
        timing::TENFOLD,
        &mut || timing::once(|| toml_string(&small)),
        &mut || timing::once(|| toml_string(&large)),
    );
    let e = ProjectFileError {
        path: large.clone(),
        reason: ErrorReason::Toml,
        detail: cps("x"),
    };
    assert!(e.line().len() > large.len());
    let p = ProjectFile {
        id: large.clone(),
        name: large,
        boundary: true,
        path: cps("/x"),
    };
    // Every code point of the text is a scalar value.
    assert!(render(&p).is_some());
}

#[test]
fn surrogates_and_the_extremes_of_the_code_space() {
    let p = ProjectFile {
        id: vec![0x10FFFF, 0, 0x7F, 0xDFFF],
        name: Vec::new(),
        boundary: false,
        path: Vec::new(),
    };
    assert_eq!(render(&p), None);
    assert_eq!(toml_string(&[0x10FFFF]), vec![34, 0x10FFFF, 34]);
}
