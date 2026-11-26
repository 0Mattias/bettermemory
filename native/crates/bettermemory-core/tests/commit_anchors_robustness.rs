//! Hostile inputs to `commit_anchors`: 100,000 paths, 1,000 recorded
//! roots, paths 4,096 components deep, a million code points, and every
//! byte value at every place a separator, a drive or a dot is read. None
//! may panic, in debug or release, and the cost grows linearly in the
//! paths and in the roots (the reference tries each root per path).

mod timing;

use bettermemory_core::commit_anchors::{
    expected_home, project_commit_anchors, relative_under, PathHost,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify_anchoring::anchor::ClaimAnchor;
use bettermemory_core::verify_candidates::scan::UserHome;

fn host() -> PathHost {
    PathHost {
        nt: false,
        cwd: Ok(cps("/w/cwd")),
    }
}

fn anchor(roots: Vec<String>) -> ClaimAnchor {
    let mut a = ClaimAnchor::new(Some("/live".into()), "home_checkout");
    a.recorded_roots = roots;
    a
}

fn project(paths: &[PyStr], a: &ClaimAnchor) -> (usize, bool) {
    let mut lookup = |_: &[u32]| UserHome::Unknown;
    let got = project_commit_anchors(paths, Some(a), &host(), Some(&cps("/hd/u")), &mut lookup)
        .expect("no failure");
    (got.paths.len(), got.unmapped)
}

#[test]
fn a_hundred_thousand_paths_grow_linearly() {
    let a = anchor(vec!["/old/a".into(), "/old/b".into(), "C:\\old".into()]);
    let paths = |n: usize| -> Vec<PyStr> {
        (0..n)
            .map(|i| match i % 4 {
                0 => cps(&format!("/old/a/{i}")),
                1 => cps(&format!("/else/{i}")),
                2 => cps(&format!("C:\\old\\{i}")),
                _ => cps(&format!("rel/{i}")),
            })
            .collect()
    };
    let (small, large) = (paths(10_000), paths(100_000));
    assert_eq!(project(&large, &a), (100_000, false));
    timing::grows_within(
        "paths",
        timing::TENFOLD,
        &mut || timing::once(|| project(&small, &a)),
        &mut || timing::once(|| project(&large, &a)),
    );
}

#[test]
fn a_thousand_recorded_roots_grow_linearly() {
    let roots = |n: usize| -> Vec<String> { (0..n).map(|i| format!("/r/{i:05}")).collect() };
    let paths: Vec<PyStr> = (0..200).map(|i| cps(&format!("/none/{i}"))).collect();
    let (small, large) = (anchor(roots(100)), anchor(roots(1_000)));
    assert_eq!(project(&paths, &large), (200, false));
    let under: Vec<PyStr> = vec![cps("/r/00999/x"), cps("/r/00000/y")];
    assert_eq!(project(&under, &large), (2, false));
    timing::grows_within(
        "roots",
        timing::TENFOLD,
        &mut || timing::once(|| project(&paths, &small)),
        &mut || timing::once(|| project(&paths, &large)),
    );
}

#[test]
fn paths_four_thousand_components_deep() {
    let deep = "/d".repeat(4_096);
    let path = cps(&format!("{deep}/x"));
    let h = host();
    assert_eq!(
        relative_under(&path, Some(&cps(&deep)), &h),
        Ok(Some(cps("x")))
    );
    let nt = cps(&format!("C:{}\\x", "\\d".repeat(4_096)));
    let nt_root = cps(&format!("C:{}", "\\d".repeat(4_096)));
    assert_eq!(relative_under(&nt, Some(&nt_root), &h), Ok(Some(cps("x"))));
    let parents = cps(&format!("/old{}", "/..".repeat(4_096)));
    assert_eq!(relative_under(&parents, Some(&cps("/old")), &h), Ok(None));
    assert!(expected_home(&path, &[cps("/d")]));
    let a = anchor(vec![deep.clone(), "/d".into()]);
    assert_eq!(project(&[path, parents], &a), (2, false));
}

#[test]
fn a_million_code_points() {
    let long = cps(&format!("/old/{}", "a".repeat(1_000_000)));
    let a = anchor(vec!["/old".into()]);
    assert_eq!(project(std::slice::from_ref(&long), &a), (1, false));
    let h = host();
    assert!(relative_under(&long, Some(&long), &h).unwrap().is_some());
}

#[test]
fn every_byte_value_in_a_path() {
    let h = host();
    let a = anchor(vec!["/old".into(), "C:\\old".into(), "rel".into()]);
    for b in 0u32..=255 {
        for shape in [
            vec!['/' as u32, b],
            vec![b, ':' as u32, '\\' as u32, b],
            vec!['C' as u32, ':' as u32, b, b],
            vec!['\\' as u32, b, '\\' as u32, b],
            vec!['~' as u32, b, '/' as u32],
            vec!['.' as u32, b, '.' as u32],
        ] {
            let mut path = cps("/old/");
            path.extend_from_slice(&shape);
            for root in ["/old", "C:\\old", "rel", "\\", "", "C:", "\\\\s\\h"] {
                let root = cps(root);
                let _ = relative_under(&path, Some(&root), &h);
                let _ = relative_under(&shape, Some(&root), &h);
                let _ = relative_under(&root, Some(&shape), &h);
                let _ = expected_home(&shape, &[root]);
            }
            let mut lookup = |_: &[u32]| UserHome::Found(vec![b]);
            let _ = project_commit_anchors(
                &[path, shape.clone()],
                Some(&a),
                &h,
                Some(&[b]),
                &mut lookup,
            );
        }
    }
}
