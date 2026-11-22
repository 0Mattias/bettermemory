//! Every case of `mirror_format.jsonl` against `mirror_format` and
//! `native_text`: the tombstone names, `origin_projection` and
//! `has_surrogates`, the metadata mappings of memories and episodes,
//! `memory_filename` and the constants.

mod common;

use bettermemory_core::mirror_format as mf;
use bettermemory_core::native_text::{has_surrogates, origin_projection};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::{Episode, Memory};
use bettermemory_core::unicode::cps;
use common::{jsonl, neutral, text_field};

const FILE: &str = "mirror_format.jsonl";

#[test]
fn mirror_format_every_case() {
    let rows = jsonl(FILE);
    let mut checked = 0;
    for row in &rows {
        match row["k"].as_str().expect("a kind") {
            "names" => {
                let n = text_field(row, "n");
                assert_eq!(
                    mf::active_filename_for_tombstone(&n),
                    text_field(row, "a"),
                    "{row}"
                );
                assert_eq!(
                    mf::is_legacy_tombstone_name(&n),
                    row["l"].as_bool().unwrap(),
                    "{row}"
                );
                if let Some(id) = row.get("id") {
                    let id = cps(id.as_str().unwrap());
                    assert_eq!(
                        mf::tombstone_filename(&n, &id),
                        text_field(row, "t"),
                        "{row}"
                    );
                }
            }
            "origin" => {
                let whole = neutral(&row["o"]);
                let PyValue::Dict(o) = &whole else {
                    panic!("an origin mapping")
                };
                assert_eq!(has_surrogates(&whole), row["h"].as_bool().unwrap());
                let (legacy, native) = origin_projection(o).expect("encodes");
                let got = PyValue::Tuple(vec![
                    PyValue::Dict(legacy),
                    native.map_or(PyValue::None, PyValue::Str),
                ]);
                assert_eq!(got, neutral(&row["r"]), "{row}");
            }
            "memory" => {
                let memory = Memory::from_payload(&neutral(&row["in"])).expect("a memory");
                assert_eq!(mf::memory_metadata(&memory), neutral(&row["out"]), "{row}");
                assert_eq!(mf::memory_filename(&memory), text_field(row, "f"), "{row}");
            }
            "episode" => {
                let episode = Episode::from_payload(&neutral(&row["in"])).expect("an episode");
                assert_eq!(
                    mf::episode_metadata(&episode),
                    neutral(&row["out"]),
                    "{row}"
                );
            }
            "constants" => {
                let v = &row["v"];
                assert_eq!(v[0], mf::TOMBSTONE_DIR);
                assert_eq!(v[1], mf::EPISODES_DIR);
                assert_eq!(v[2], mf::TOMBSTONE_SUFFIX);
                let keys = |i: usize| -> Vec<String> {
                    v[i].as_array()
                        .unwrap()
                        .iter()
                        .map(|s| s.as_str().unwrap().to_owned())
                        .collect()
                };
                assert_eq!(keys(3), mf::MEMORY_METADATA_KEYS);
                assert_eq!(keys(4), mf::PATH_TARGET_METADATA_KEYS);
                assert_eq!(keys(5), mf::TOMBSTONE_METADATA_KEYS);
                assert_eq!(keys(6), mf::EPISODE_METADATA_KEYS);
            }
            other => panic!("unknown kind {other}"),
        }
        checked += 1;
    }
    common::assert_count("mirror_format", checked, rows.len());
}

/// `memory_metadata` writes `verified_head` only when it is truthy. The
/// reference's validator refuses an empty head, so no golden row holds
/// one; the Rust fields can be set directly, and an empty head must drop
/// the key exactly as an absent one does.
#[test]
fn an_empty_verified_head_drops_the_key() {
    let mut seen = 0;
    for row in jsonl(FILE).iter().filter(|r| r["k"] == "memory") {
        let mut memory = Memory::from_payload(&neutral(&row["in"])).expect("a memory");
        memory.fields.verified_head = None;
        let absent = mf::memory_metadata(&memory);
        memory.fields.verified_head = Some(Vec::new());
        assert_eq!(mf::memory_metadata(&memory), absent, "{row}");
        memory.fields.verified_head = Some(cps("abc"));
        assert_ne!(mf::memory_metadata(&memory), absent, "{row}");
        seen += 1;
    }
    assert!(seen > 0);
}
