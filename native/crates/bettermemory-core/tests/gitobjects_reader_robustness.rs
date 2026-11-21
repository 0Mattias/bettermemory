//! Hostile questions and stores for `gitobjects::files::reader`: many
//! names, crowded fanout folders, very long and non-ASCII names, a large
//! object, a long delta chain and caps at their integer bounds. No panic in
//! debug or release; the costs grow as the reference's do.

mod common;
mod timing;

use bettermemory_core::fsdata::{MemFs, Node};
use common::gitobjects_reader::{
    empty_repo, entry_head, index_of, loose_file, loose_path, name_of, reader_of, stored,
    unhex_text,
};

const OBJ: &str = "/m/g/objects";

fn names(n: usize) -> Vec<String> {
    (0..n)
        .map(|i| format!("{:040x}", (i as u128) * 0x9e37_79b9_7f4a_7c15_f39c))
        .collect()
}

fn crowded(n: usize) -> MemFs {
    let mut fs = empty_repo();
    for i in 0..n {
        let name = format!("ab{:038x}", i + 1);
        fs.insert(&loose_path(OBJ, &name), Node::File(Vec::new()));
    }
    fs
}

#[test]
fn has_many_grows_linearly() {
    let fs = crowded(10);
    let (small, large) = (names(2_000), names(20_000));
    let small: Vec<&str> = small.iter().map(String::as_str).collect();
    let large: Vec<&str> = large.iter().map(String::as_str).collect();
    assert!(reader_of(&fs).has_many(&large).unwrap().is_empty());
    timing::grows_within(
        "has_many",
        timing::TENFOLD,
        &mut || timing::once(|| reader_of(&fs).has_many(&small)),
        &mut || timing::once(|| reader_of(&fs).has_many(&large)),
    );
}

#[test]
fn resolve_over_a_crowded_fanout_grows_linearly() {
    let (small, large) = (crowded(2_000), crowded(20_000));
    assert_eq!(reader_of(&large).resolve("abff").unwrap(), None);
    timing::grows_within(
        "resolve",
        timing::TENFOLD,
        &mut || timing::once(|| reader_of(&small).resolve("abff")),
        &mut || timing::once(|| reader_of(&large).resolve("abff")),
    );
}

#[test]
fn hostile_names_answer_without_panic() {
    let fs = crowded(3);
    let mut reader = reader_of(&fs);
    let long = "a".repeat(1 << 20);
    let wide = "\u{e9}".repeat(40);
    let emoji = "\u{1f600}".repeat(4);
    for text in [long.as_str(), wide.as_str(), emoji.as_str(), "", "\0\0\0\0"] {
        assert!(!reader.has(text).unwrap());
        assert_eq!(reader.resolve(text).unwrap(), None);
        assert_eq!(reader.header(text).unwrap(), None);
        assert_eq!(reader.read(text, u64::MAX).unwrap(), None);
    }
    let name = format!("ab{:038x}", 1);
    // An empty file is no object, under every cap.
    for cap in [0, 1, u64::MAX] {
        assert_eq!(reader.read(&name, cap).unwrap(), None);
    }
}

#[test]
fn a_large_object_and_a_long_chain_answer_within_bounds() {
    let mut fs = empty_repo();
    let big = vec![b'x'; 4 << 20];
    let name = name_of("aa", '1', 20);
    fs.insert(
        &loose_path(OBJ, &name),
        Node::File(loose_file("blob", &big)),
    );
    // A chain of 5,000 OFS deltas, each on the entry before it.
    let mut pack = b"PACK\0\0\0\x02\0\0\0\0".to_vec();
    pack.extend(entry_head(3, 1));
    pack.extend(stored(b"a"));
    let mut prev = 12u64;
    let mut entries = vec![(unhex_text(&name_of("bb", '0', 20)), 12u64)];
    let mut top = String::new();
    for i in 0..5_000u32 {
        let at = pack.len() as u64;
        let delta = [1u8, 1, 0x90, 1];
        pack.extend(entry_head(6, delta.len()));
        let mut back = at - prev;
        let mut ofs = vec![(back & 0x7F) as u8];
        back >>= 7;
        while back != 0 {
            back -= 1;
            ofs.push(0x80 | (back & 0x7F) as u8);
            back >>= 7;
        }
        ofs.reverse();
        pack.extend(ofs);
        pack.extend(stored(&delta));
        top = format!("cc{i:038x}");
        entries.push((unhex_text(&top), at));
        prev = at;
    }
    fs.insert(format!("{OBJ}/pack/p.pack").as_bytes(), Node::File(pack));
    fs.insert(
        format!("{OBJ}/pack/p.idx").as_bytes(),
        Node::File(index_of(&entries, 20)),
    );
    let mut reader = reader_of(&fs);
    assert_eq!(
        reader.read(&name, 4 << 20).unwrap().unwrap().1.len(),
        4 << 20
    );
    assert_eq!(reader.read(&name, (4 << 20) - 1).unwrap(), None);
    assert_eq!(reader.header(&top).unwrap(), None);
    assert_eq!(reader.read(&top, u64::MAX).unwrap(), None);
    let below = format!("cc{:038x}", 4_000);
    assert_eq!(reader.read(&below, u64::MAX).unwrap().unwrap().1, b"a");
    timing::ends_within(
        "a large object and a long chain",
        std::time::Duration::from_secs(20),
        &mut || {
            timing::wall(|| {
                let mut reader = reader_of(&fs);
                (
                    reader.read(&name, 4 << 20).unwrap(),
                    reader.read(&below, u64::MAX).unwrap(),
                )
            })
        },
    );
}
