//! Cases a plausible wrong port of `location_git`'s readers fails: git's
//! four whitespace bytes for Python's ten, a whole read for the first 128
//! bytes, a split on one space, a link to a folder entered, the manifest
//! bounded after its sort, a 64-bit mask, a 64-bit clock, an empty head
//! read as a head, a peeled hash kept in its case.

mod common;

use bettermemory_core::capture::Kind;
use bettermemory_core::fsdata::{Flavour, PyErr};
use bettermemory_core::gitfile_rule::hostpath::text::Process;
use bettermemory_core::location_git::{
    manifest, reachable_among, ref_tips, resolve_head, walk, Git, EDGE_LIMIT,
};
use bettermemory_core::pyvalue::PyStr;
use common::location_git::{hash, mem_tree, ref_tree, Entry, Hist};

struct Root;

impl Process for Root {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        Ok(vec![u32::from(b'/')])
    }
}

fn tips(items: Vec<Entry>) -> Vec<String> {
    ref_tips(&mem_tree(&items, b"/s/g"), b"/s/g")
}

fn file(rel: &str, data: Vec<u8>) -> Entry {
    Entry::File(rel.as_bytes().to_vec(), data)
}

#[test]
fn every_python_whitespace_byte_is_stripped() {
    let h = hash("k", 0);
    for b in [0x0bu8, 0x0c, 0x1c, 0x1d, 0x1e, 0x1f] {
        let data = [vec![b], h.clone().into_bytes(), vec![b]].concat();
        assert_eq!(
            tips(vec![file("refs/heads/m", data)]),
            vec![h.clone()],
            "{b:#x}"
        );
    }
    assert!(tips(vec![file("refs/heads/m", [h.as_bytes(), b"\x85"].concat())]).is_empty());
}

#[test]
fn only_the_first_128_bytes_are_read() {
    let h = hash("k", 1);
    let data = [h.as_bytes(), &[b' '; 88], b"junk"].concat();
    assert_eq!(tips(vec![file("refs/heads/m", data)]), vec![h]);
}

#[test]
fn packed_refs_split_on_runs_of_any_whitespace() {
    let h = hash("k", 2);
    let data = format!("{h}\t\x0b refs/remotes/o/x\r\n").into_bytes();
    assert_eq!(tips(vec![file("packed-refs", data)]), vec![h]);
}

#[test]
fn a_link_to_a_folder_is_not_entered() {
    let items = vec![
        file("elsewhere/x", hash("k", 3).into_bytes()),
        Entry::Link(b"refs/heads/l".to_vec(), b"../../elsewhere".to_vec()),
    ];
    assert!(tips(items).is_empty());
}

#[test]
fn the_manifest_bound_follows_the_walk_not_the_sort() {
    let mut items: Vec<Entry> = (0..4096)
        .map(|i| file(&format!("f{i:04}"), vec![b'x']))
        .collect();
    items.push(file("a/first", vec![]));
    let fs = mem_tree(&items, b"/s/proj");
    let m = manifest(&fs, Flavour::Posix, &Root, b"/s/proj").unwrap();
    assert_eq!(m.entries.len(), 4096);
    assert!(m.entries.iter().all(|(p, _)| p[0] == u32::from(b'f')));
}

#[test]
fn more_than_128_given_commits_each_keep_a_bit() {
    let n = 300;
    let lines: Vec<Vec<usize>> = (0..n)
        .map(|i| if i + 1 < n { vec![i + 1] } else { vec![] })
        .collect();
    let h = Hist::new(lines, false);
    let given: Vec<String> = h.ids.clone();
    let mut parents = |s: &str| h.parents(s);
    let reach = reachable_among(&mut parents, &given, 1000, EDGE_LIMIT, None);
    let root = &h.ids[n - 1];
    let (_, from) = reach.relations.iter().find(|(c, _)| c == root).unwrap();
    assert_eq!(from.len(), n - 1);
    assert!(!reach.bounded);
}

#[test]
fn committer_times_past_i64_are_compared_as_integers() {
    let h = Hist::new(vec![vec![1], vec![2], vec![]], false);
    let big = (1i128 << 64) - 1;
    let times = [big, 0, big];
    let mut parents = |s: &str| h.parents(s);
    let mut committed = |s: &str| times[h.at(s).unwrap()];
    let given = vec![h.ids[0].clone(), h.ids[2].clone()];
    let reach = reachable_among(&mut parents, &given, 512, EDGE_LIMIT, Some(&mut committed));
    let (_, from) = reach
        .relations
        .iter()
        .find(|(c, _)| *c == h.ids[2])
        .unwrap();
    assert_eq!(from, &vec![h.ids[0].clone()]);
}

#[test]
fn an_empty_head_walks_the_tips() {
    let h = Hist::new(vec![vec![], vec![]], false);
    let mut parents = |s: &str| h.parents(s);
    let mut fp = |s: &str| h.fingerprint(s);
    let out = walk(&mut parents, &mut fp, Some(""), &[h.ids[1].clone()]);
    assert_eq!(out.len(), 1);
    assert_eq!(out[0].0, h.ids[1]);
}

#[test]
fn a_peeled_hash_is_lowercased() {
    // `_resolve_head` lowercases whatever the peel returns.
    let fs = ref_tree(
        &[
            file("HEAD", b"ref: refs/tags/v1\n".to_vec()),
            file("refs/tags/v1", hash("t", 0).into_bytes()),
        ],
        b"s/g",
    );
    let git = Git {
        dotgit: None,
        git_dir: b"s/g".to_vec(),
        common_dir: b"s/g".to_vec(),
        root: None,
        kind: Kind::Primary,
    };
    let upper = "ABCDEF0123456789ABCDEF0123456789ABCDEF01";
    let mut peel = |_: &str| Some(upper.to_owned());
    let (sha, _) = resolve_head(&fs, &git, &mut peel);
    assert_eq!(sha.as_deref(), Some(upper.to_lowercase().as_str()));
}
