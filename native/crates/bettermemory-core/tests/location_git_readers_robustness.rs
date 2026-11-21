//! Hostile input to `location_git`'s readers: deep and wide trees, a large
//! packed-refs file, long chains and an octopus past `EDGE_LIMIT`. No
//! panic, and time that grows with the input.

mod common;
mod timing;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFile, MemFs, OsError, PyErr, Stat, StatResult,
};
use bettermemory_core::gitfile_rule::hostpath::text::Process;
use bettermemory_core::location_git::{manifest, reachable_among, ref_tips, walk, EDGE_LIMIT};
use bettermemory_core::pyvalue::PyStr;
use common::location_git::{hash, mem_tree, Entry, Hist};
use std::cell::Cell;

struct Root;

impl Process for Root {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        Ok(vec![u32::from(b'/')])
    }
}

#[test]
fn a_deep_tree_is_walked_without_recursion() {
    // Within `PATH_MAX` (1,024 on macOS): past it the OS refuses the path.
    let deep: Vec<u8> = b"d/".repeat(400);
    let items = vec![
        Entry::File([&deep[..], b"x"].concat(), vec![b'1']),
        Entry::File(
            [b"refs/heads/".as_slice(), &deep, b"m"].concat(),
            hash("r", 0).into_bytes(),
        ),
    ];
    let fs = mem_tree(&items, b"/s");
    let m = manifest(&fs, Flavour::Posix, &Root, b"/s").unwrap();
    assert_eq!(m.entries.len(), 2);
    assert_eq!(ref_tips(&fs, b"/s"), vec![hash("r", 0)]);
}

fn packed(n: usize) -> Vec<Entry> {
    let mut data = Vec::new();
    for i in 0..n {
        data.extend(format!("{} refs/tags/t{i}\n", hash("p", i)).bytes());
    }
    vec![Entry::File(b"packed-refs".to_vec(), data)]
}

#[test]
fn a_large_packed_refs_reads_in_linear_time() {
    let (small, large) = (
        mem_tree(&packed(2_000), b"/s"),
        mem_tree(&packed(20_000), b"/s"),
    );
    timing::grows_within(
        "packed-refs",
        30,
        &mut || timing::once(|| ref_tips(&small, b"/s")),
        &mut || timing::once(|| ref_tips(&large, b"/s")),
    );
}

fn chain(n: usize) -> Hist {
    Hist::new(
        (0..n)
            .map(|i| if i + 1 < n { vec![i + 1] } else { vec![] })
            .collect(),
        false,
    )
}

#[test]
fn an_octopus_past_the_edge_bound_stops() {
    let n = 300_000;
    let mut lines = vec![(1..n).collect::<Vec<_>>()];
    lines.extend((1..n).map(|_| Vec::new()));
    let h = Hist::new(lines, false);
    let mut parents = |s: &str| h.parents(s);
    let given = vec![h.ids[0].clone(), h.ids[1].clone()];
    let reach = reachable_among(&mut parents, &given, 512, EDGE_LIMIT, None);
    assert!(reach.bounded);
    assert!(reach.handled <= EDGE_LIMIT + n as u64);
}

#[test]
fn many_given_commits_spread_in_bounded_time() {
    let (small, large) = (chain(60), chain(600));
    let run = |h: &Hist| {
        let mut parents = |s: &str| h.parents(s);
        let given = h.ids.clone();
        reachable_among(&mut parents, &given, 10_000, EDGE_LIMIT, None)
    };
    // Each commit's mask holds every given commit above it: the work is
    // quadratic in the given commits, as the reference's integers are.
    timing::grows_within(
        "many given",
        400,
        &mut || timing::once(|| run(&small)),
        &mut || timing::once(|| run(&large)),
    );
}

#[test]
fn extreme_times_do_not_overflow() {
    let h = chain(5);
    let times = [0i128, (1 << 64) - 1, 0, (1 << 64) - 1, 0];
    let mut parents = |s: &str| h.parents(s);
    let mut committed = |s: &str| times[h.at(s).unwrap()];
    let given = h.ids.clone();
    let reach = reachable_among(&mut parents, &given, 3, 2, Some(&mut committed));
    assert_eq!(reach.relations.len(), 5);
}

#[test]
fn a_long_walk_stops_at_the_limit() {
    let h = chain(100_000);
    let mut parents = |s: &str| h.parents(s);
    let mut fp = |s: &str| h.fingerprint(s);
    let out = walk(&mut parents, &mut fp, Some(&h.ids[0]), &[]);
    assert_eq!(out.len(), 512);
}

/// A [`MemFs`] that counts its reads and fails every read after `fail_after`.
struct Counted {
    fs: MemFs,
    reads: Cell<usize>,
    fail_after: usize,
}

impl Fs for Counted {
    type File = MemFile;
    fn open(&self, path: &[u8]) -> Result<MemFile, PyErr> {
        self.fs.open(path)
    }
    fn fstat(&self, file: &mut MemFile) -> Result<Stat, PyErr> {
        self.fs.fstat(file)
    }
    fn read(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, PyErr> {
        self.reads.set(self.reads.get() + 1);
        if self.reads.get() > self.fail_after {
            return Err(PyErr::Os);
        }
        self.fs.read(file, max)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.fs.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.fs.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.fs.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.fs.readlink(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.fs.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.fs.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.fs.listdir(path)
    }
}

/// 64 branch tips first, then `filler` bytes of junk lines.
fn tips_then_filler(filler: usize) -> MemFs {
    let mut data = Vec::new();
    for i in 0..64 {
        data.extend(format!("{} refs/heads/b{i}\n", hash("p", i)).bytes());
    }
    let junk = b"junk line that is no ref\n";
    while data.len() < filler {
        data.extend_from_slice(junk);
    }
    mem_tree(&[Entry::File(b"packed-refs".to_vec(), data)], b"/s")
}

#[test]
fn packed_refs_stops_reading_at_the_last_tip() {
    // `for raw in fh` ... `break`: the reference reads no further than the
    // 64th tip, so a file of any size behind it costs one chunk.
    let fs = Counted {
        fs: tips_then_filler(8 << 20),
        reads: Cell::new(0),
        fail_after: usize::MAX,
    };
    assert_eq!(ref_tips(&fs, b"/s").len(), 64);
    assert_eq!(fs.reads.get(), 1);
}

#[test]
fn a_failed_read_keeps_the_packed_tips_before_it() {
    // An `OSError` partway through leaves the set the loop already filled.
    let mut data = Vec::new();
    for i in 0..10 {
        data.extend(format!("{} refs/heads/b{i}\n", hash("p", i)).bytes());
    }
    data.resize(1 << 16, b'x');
    data.extend(format!("\n{} refs/heads/late\n", hash("q", 0)).bytes());
    let fs = Counted {
        fs: mem_tree(&[Entry::File(b"packed-refs".to_vec(), data)], b"/s"),
        reads: Cell::new(0),
        fail_after: 1,
    };
    let mut want: Vec<String> = (0..10).map(|i| hash("p", i)).collect();
    want.sort();
    assert_eq!(ref_tips(&fs, b"/s"), want);
}

#[test]
fn times_at_the_i128_limits_compare_as_python_integers() {
    // `t - when` of i128::MAX and i128::MIN needs 129 bits; Python's
    // integers hold it, and the port compares without forming it.
    let h = chain(2);
    let times = [i128::MIN, i128::MAX];
    let mut parents = |s: &str| h.parents(s);
    let mut committed = |s: &str| times[h.at(s).unwrap()];
    let given = h.ids.clone();
    let reach = reachable_among(&mut parents, &given, 512, EDGE_LIMIT, Some(&mut committed));
    assert_eq!(reach.relations.len(), 2);
    assert!(!reach.bounded);
}
