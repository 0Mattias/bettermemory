//! Hostile input for `location::capture`: a cache churned far past its
//! cap, walks of many files, every `mode_t` width; no panic, and time
//! that grows with the input as the reference's does.

mod location_capture_world;
mod timing;

use bettermemory_core::location::capture::{
    file_tracked, s_isdir, walk_files, AboveCache, ObjectReads, ProjectFiles, Walk, CACHE_CAP,
    MODE_T_BITS,
};
use location_capture_world::cps;

struct Many(usize);

impl ProjectFiles for Many {
    type Above = usize;
    fn above_start(&mut self, _cwd: &[u32], root: &[u32]) -> Option<Vec<u32>> {
        Some(root.to_vec())
    }
    fn find(&mut self, _cwd: &[u32], _root: Option<&[u32]>, _a: Option<&usize>) -> Walk<usize> {
        let f = bettermemory_core::location::capture::ProjectFile {
            id: cps("bmp_1"),
            name: cps("n"),
            directory: cps("/d"),
            path: cps("/d/bettermemory.toml"),
            boundary: false,
        };
        Walk {
            files: vec![f; self.0],
            errors: vec![vec![0x10FFFF; 64]; self.0],
            above: self.0,
            folder: None,
            root: None,
        }
    }
}

fn churn(puts: usize) {
    let cache = AboveCache::new(CACHE_CAP);
    for i in 0..puts {
        let root = cps(&format!("/r{}", i % (CACHE_CAP * 3)));
        walk_files(
            &mut Many(0),
            &cache,
            &mut || (1.0, 1.0),
            &mut |_| Ok(None),
            &cps("/x"),
            Some(&root),
        )
        .unwrap();
    }
    assert!(cache.keys().len() <= CACHE_CAP);
}

#[test]
fn the_cache_stays_within_its_cap_and_grows_linearly() {
    churn(10_000);
    timing::grows_within(
        "cache: 2,000 and 20,000 walks",
        timing::TENFOLD,
        &mut || timing::once(|| churn(2_000)),
        &mut || timing::once(|| churn(20_000)),
    );
}

#[test]
fn a_walk_of_many_files() {
    let cache = AboveCache::new(1);
    let run = |n: usize| {
        let (_, seen, errors) = walk_files(
            &mut Many(n),
            &cache,
            &mut || (f64::INFINITY, f64::NAN),
            &mut |_| Ok(None),
            &cps("/x"),
            None,
        )
        .unwrap();
        assert_eq!((seen.len(), errors.len()), (n, n));
    };
    run(200_000);
    timing::grows_within(
        "walk: 10,000 and 100,000 files",
        timing::TENFOLD,
        &mut || timing::once(|| run(10_000)),
        &mut || timing::once(|| run(100_000)),
    );
}

#[test]
fn every_mode_width_answers() {
    for bits in [0, 1, 15, 16, 31, 32, 40] {
        for mode in [0, 1, 0o040000, 0xFFFF, 0x10000, u32::MAX] {
            let _ = s_isdir(mode, bits);
        }
    }
}

struct Wide;

impl ObjectReads for Wide {
    fn commit(&mut self, _c: &[u32], _s: &[u32]) -> Option<Vec<u32>> {
        Some(cps("t"))
    }
    fn tree(&mut self, _c: &[u32], _s: &[u32]) -> Option<Vec<(Vec<u8>, u32)>> {
        let mut v = vec![(b"x".to_vec(), u32::MAX); 500_000];
        v.push((b"bettermemory.toml".to_vec(), u32::MAX));
        Some(v)
    }
}

#[test]
fn a_tree_of_many_wide_entries() {
    let got = file_tracked(&mut Wide, MODE_T_BITS, &cps("/c"), &[0x10FFFF; 1 << 16]);
    if MODE_T_BITS == 16 {
        assert!(got.is_err());
    } else {
        assert_eq!(got, Ok(Some(true)));
    }
}
