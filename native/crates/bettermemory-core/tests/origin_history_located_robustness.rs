//! Hostile input for `origin_history::located`: shallow files at the
//! bound, of many records, of one long record, of every byte; long and
//! odd paths. No panic, and `cuts_in` grows linearly with the file.

mod common;
mod timing;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::origin_history::{cuts_in, Cut, ValueError, CUTS_MAX};

fn read(data: Vec<u8>, path: &[u8]) -> Result<Option<Cut>, ValueError> {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/c", Node::Dir);
    fs.insert(
        b"/c/config",
        Node::File(b"[core]\n\trepositoryformatversion = 0\n".to_vec()),
    );
    fs.insert(b"/c/shallow", Node::File(data));
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    let git = GitFacts {
        release: Some((2, 54)),
        reads_compat: Some(true),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &git,
    };
    cuts_in(&rule, path)
}

fn records(n: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(n * 41);
    for i in 0..n {
        out.extend_from_slice(format!("{i:040x}\n").as_bytes());
    }
    out
}

#[test]
fn the_most_records_the_bound_holds() {
    let n = CUTS_MAX / 41;
    let got = read(records(n), b"/c/shallow").unwrap().unwrap();
    assert_eq!(got.len(), n);
}

#[test]
fn one_record_of_the_whole_bound() {
    let mut data = vec![b'A'; CUTS_MAX];
    data[40] = 0xff;
    assert_eq!(read(data, b"/c/shallow").unwrap().map(|s| s.len()), Some(1));
}

#[test]
fn every_byte_as_a_first_byte() {
    for byte in 0..=255u8 {
        let mut data = vec![byte];
        data.extend_from_slice(&[b'0'; 39]);
        let got = read(data, b"/c/shallow").unwrap();
        let hex = byte.is_ascii_hexdigit();
        assert_eq!(got.is_some(), hex, "{byte}");
    }
}

#[test]
fn only_newlines_and_only_carriage_returns() {
    assert_eq!(read(vec![b'\n'; CUTS_MAX], b"/c/shallow").unwrap(), None);
    assert_eq!(read(vec![b'\r'; CUTS_MAX], b"/c/shallow").unwrap(), None);
}

#[test]
fn long_and_odd_paths() {
    let deep: Vec<u8> = b"/c"
        .iter()
        .copied()
        .chain(b"/.".repeat(100_000))
        .chain(b"/shallow".iter().copied())
        .collect();
    assert_eq!(read(records(1), &deep).unwrap().map(|s| s.len()), Some(1));
    let slashes: Vec<u8> = b"/c"
        .iter()
        .copied()
        .chain(vec![b'/'; 100_000])
        .chain(b"shallow".iter().copied())
        .collect();
    assert_eq!(
        read(records(1), &slashes).unwrap().map(|s| s.len()),
        Some(1)
    );
    assert_eq!(read(records(1), b"").unwrap(), None);
    assert_eq!(read(records(1), b"/").unwrap(), None);
    assert_eq!(read(records(1), b"\0").err(), Some(ValueError));
}

#[test]
fn cuts_in_grows_linearly() {
    let small = records(20_000);
    let large = records(200_000);
    timing::grows_within(
        "cuts_in: 20,000 and 200,000 records",
        timing::TENFOLD,
        &mut || timing::once(|| read(small.clone(), b"/c/shallow")),
        &mut || timing::once(|| read(large.clone(), b"/c/shallow")),
    );
}

#[cfg(unix)]
mod disk {
    use crate::common::origin_history_located::{answer, write_tree, Scratch, State};
    use serde_json::{json, Value};

    #[test]
    fn a_deep_start_and_long_names_do_not_panic() {
        let base = Scratch::new("robust");
        let dir = base.0.join("t");
        std::fs::create_dir(&dir).unwrap();
        write_tree(
            &json!({"f": {"r/.git/HEAD": "ref: refs/heads/main\n", "r/.git/objects/.keep": "", "r/.git/refs/.keep": ""}}),
            &dir,
        );
        let root = dir.to_str().unwrap().to_owned();
        let mut state = State::new();
        let deep = format!("<root>/r{}", "/x".repeat(5_000));
        let long = format!("<root>/r/{}", "n".repeat(300));
        for q in ["K:", "G:", "C:"] {
            for start in [&deep, &long] {
                let query = if q == "C:" {
                    format!("{q}{start}|W")
                } else {
                    format!("{q}{start}")
                };
                let got = answer(&query, &root, &mut state);
                assert!(
                    got.is_null() || got.is_object() || got.is_array(),
                    "{query}"
                );
            }
        }
        assert_eq!(answer("K:<root>/r", &root, &mut state), Value::Null);
        assert_eq!(answer("C:<root>/r|", &root, &mut state), json!([]));
    }
}
