//! The walk and `write` on hostile input: deep paths, a NUL, a lone
//! surrogate, a very long name. No panic in debug or release.
//!
//! The walk is quadratic in the depth, as the reference's is: each
//! `Path.parent` copies the text above it. Ten times the depth costs
//! under three hundred times the time (a hundred for quadratic, with
//! room for a loaded machine), so a cubic walk fails.

mod timing;

use std::time::Duration;

use bettermemory_core::fsdata::{Flavour, MemFs, Node, OsError, PyErr, StatResult};
use bettermemory_core::gitfile_rule::hostpath::text::Process;
use bettermemory_core::project_file::{Found, ProjectFile};
use bettermemory_core::project_walk::{find, write, Fault, WriteEffects};
use bettermemory_core::pyvalue::PyStr;

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

struct Root;

impl Process for Root {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        Ok(cps("/"))
    }
}

fn deep(depth: usize) -> (MemFs, PyStr) {
    let mut fs = MemFs::new(Flavour::Posix);
    let path = format!("/{}", vec!["d"; depth].join("/"));
    fs.insert(path.as_bytes(), Node::Dir);
    fs.insert(b"/bettermemory.toml", Node::File(b"x".to_vec()));
    (fs, cps(&path))
}

fn walk_once(fs: &MemFs, cwd: &[u32]) -> Duration {
    let mut look = |path: &[u32], _: &StatResult| -> Result<Found, Fault> {
        Ok(Found::File(ProjectFile {
            id: cps("bmp_0123456789ABCDEFGHJKMNPQRS"),
            name: cps("n"),
            boundary: false,
            path: path.to_vec(),
        }))
    };
    timing::once(|| {
        let w = find(fs, Flavour::Posix, &Root, cwd, None, None, &mut look).unwrap();
        assert_eq!(w.files().len(), 1);
    })
}

#[test]
fn a_deep_walk_grows_as_the_reference_does() {
    let (small_fs, small) = deep(30);
    let (large_fs, large) = deep(300);
    timing::grows_within(
        "project_walk depth",
        300,
        &mut || walk_once(&small_fs, &small),
        &mut || walk_once(&large_fs, &large),
    );
}

/// The reference's walk to the root at depth 2,000 (`Path.parent` from
/// `/d/.../d` up) took 1.2 s on the machine this was measured on, and
/// 57 s at depth 20,000. The port's walk at depth 2,000 ends within the
/// reference's time.
#[test]
fn a_walk_as_deep_as_the_reference_was_timed_ends_within_its_time() {
    let (fs, cwd) = deep(2_000);
    timing::ends_within(
        "project_walk depth 2000",
        Duration::from_millis(1_200),
        &mut || timing::wall(|| walk_once(&fs, &cwd)),
    );
}

#[test]
fn a_nul_raises_and_a_lone_surrogate_cannot_be_encoded() {
    let fs = MemFs::new(Flavour::Posix);
    let mut look = |_: &[u32], _: &StatResult| -> Result<Found, Fault> { unreachable!() };
    let nul = find(
        &fs,
        Flavour::Posix,
        &Root,
        &cps("/a\0b"),
        None,
        None,
        &mut look,
    );
    assert!(matches!(nul, Err(Fault::Os(e)) if e.class == PyErr::Value));
    let mut lone = cps("/a");
    lone.push(0xD800);
    let got = find(&fs, Flavour::Posix, &Root, &lone, None, None, &mut look);
    assert_eq!(got.unwrap_err(), Fault::Encode);
    let mut escaped = cps("/a");
    escaped.push(0xDC80);
    let got = find(&fs, Flavour::Posix, &Root, &escaped, None, None, &mut look).unwrap();
    assert!(got.files().is_empty());
}

struct Sink;

impl WriteEffects for Sink {
    type File = ();
    fn is_dir(&self, _: &[u32]) -> bool {
        true
    }
    fn create_excl(&self, _: &[u32], _: u32) -> Result<(), OsError> {
        Ok(())
    }
    fn write_once(&self, _: &mut (), data: &[u8]) -> Result<usize, OsError> {
        Ok(data.len().min(4096))
    }
    fn fsync_file(&self, _: &mut ()) {}
    fn close(&self, _: ()) -> Result<(), OsError> {
        Ok(())
    }
    fn unlink_quietly(&self, _: &[u32]) {}
    fn atomic_write_bytes(&self, _: &[u32], _: &[u8], _: u32) -> Result<(), Fault> {
        Ok(())
    }
    fn fsync_dir(&self, _: &[u32]) {}
}

#[test]
fn a_long_name_and_odd_random_bytes() {
    let name = cps(&"\u{e9}\"\\".repeat(200_000));
    let p = write(
        &Sink,
        Flavour::Posix,
        &Root,
        &cps("d"),
        &name,
        true,
        false,
        &[0xff; 17],
    )
    .unwrap();
    assert_eq!(p.path, cps("/d/bettermemory.toml"));
    let mut lone = cps("n");
    lone.push(0xDC80);
    let got = write(
        &Sink,
        Flavour::Posix,
        &Root,
        &cps("d"),
        &lone,
        false,
        false,
        &[0; 17],
    );
    assert_eq!(got.unwrap_err(), Fault::Encode);
    let short = write(
        &Sink,
        Flavour::Posix,
        &Root,
        &cps("d"),
        &cps("n"),
        false,
        false,
        &[0; 3],
    );
    assert_eq!(short.unwrap_err(), Fault::NotPorted);
}
