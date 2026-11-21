//! Hostile repositories for `gitobjects::files`: alternates files of many
//! entries, very long and NUL-laden entries, cycles of every directory to
//! every other, pack folders of many names. No panic in debug or release;
//! the costs grow as the reference's do.

mod common;
mod timing;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitobjects::files::{ObjectReader, Stop};

const FACTS: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

fn with_alternates(text: Vec<u8>) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/m/g/objects/pack", Node::Dir);
    fs.insert(b"/m/g/objects/info/alternates", Node::File(text));
    fs
}

fn dirs(fs: &MemFs) -> Result<Vec<Vec<u8>>, Stop> {
    ObjectReader::new(fs, Flavour::Posix, b"/", FACTS, b"/m/g").object_dirs()
}

fn missing_entries(n: usize) -> Vec<u8> {
    let mut text = Vec::new();
    for i in 0..n {
        text.extend_from_slice(format!("/m/nope/{i}\n").as_bytes());
    }
    text
}

#[test]
fn many_missing_entries_grow_linearly() {
    let small = with_alternates(missing_entries(2_000));
    let large = with_alternates(missing_entries(20_000));
    assert_eq!(dirs(&large).unwrap().len(), 1);
    timing::grows_within(
        "alternates entries",
        30,
        &mut || timing::once(|| dirs(&small)),
        &mut || timing::once(|| dirs(&large)),
    );
}

#[test]
fn hostile_entries_answer_without_panic() {
    let long = vec![b'a'; 1 << 20];
    let mut text = long.clone();
    text.push(b'\n');
    text.extend_from_slice(&[b'/'; 100_000]);
    text.extend_from_slice(b"\n\"\\");
    text.extend_from_slice(&[b'.'; 10_000]);
    text.extend_from_slice(b"\n../../../../../../../../..\n\"\\377\\400\"\n\0\n");
    let fs = with_alternates(text);
    // The NUL entry raises; everything before it was passed over or kept.
    assert_eq!(
        dirs(&fs),
        Err(Stop::Raised(bettermemory_core::fsdata::PyErr::Value))
    );
    let fs = with_alternates(vec![b'\n'; 1 << 20]);
    assert_eq!(dirs(&fs).unwrap().len(), 1);
}

#[test]
fn every_directory_naming_every_other_ends() {
    let n = 60;
    let mut fs = with_alternates(Vec::new());
    let mut all = Vec::new();
    for i in 0..n {
        all.extend_from_slice(format!("/m/d{i}\n").as_bytes());
    }
    fs.insert(b"/m/g/objects/info/alternates", Node::File(all.clone()));
    for i in 0..n {
        fs.insert(
            format!("/m/d{i}/info/alternates").as_bytes(),
            Node::File(all.clone()),
        );
    }
    // Depth-first: each directory once, the walk at most six deep.
    assert_eq!(dirs(&fs).unwrap().len(), n + 1);
}

#[test]
fn a_link_cycle_in_an_entry_ends() {
    let mut fs = with_alternates(b"/m/l1/objects\n".to_vec());
    fs.insert(b"/m/l1", Node::Link(b"/m/l2".to_vec()));
    fs.insert(b"/m/l2", Node::Link(b"/m/l1".to_vec()));
    assert_eq!(dirs(&fs).unwrap().len(), 1);
}

#[test]
fn a_pack_folder_of_many_names_grows_linearly() {
    let make = |n: usize| {
        let mut fs = MemFs::new(Flavour::Posix);
        fs.insert(b"/m/g/objects/pack", Node::Dir);
        for i in 0..n {
            fs.insert(
                format!("/m/g/objects/pack/{i:06}.idx").as_bytes(),
                Node::File(vec![0; 8]),
            );
            fs.insert(
                format!("/m/g/objects/pack/{i:06}.pack").as_bytes(),
                Node::File(Vec::new()),
            );
        }
        fs
    };
    let (small, large) = (make(300), make(3_000));
    let complete =
        |fs: &MemFs| ObjectReader::new(fs, Flavour::Posix, b"/", FACTS, b"/m/g").complete();
    assert_eq!(complete(&large), Ok(false));
    timing::grows_within(
        "pack names",
        30,
        &mut || timing::once(|| complete(&small)),
        &mut || timing::once(|| complete(&large)),
    );
}
