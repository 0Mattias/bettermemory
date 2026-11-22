//! Cases a plausible wrong `mirror_format` or `native_text` fails.

use bettermemory_core::mirror_format::{
    active_filename_for_tombstone, id_tombstone_stem, is_legacy_tombstone_name, tombstone_filename,
};
use bettermemory_core::native_text::{encoded_json, has_surrogates, origin_projection};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

const ID: &str = "01HZX3K9QG7T2M5V8B4N6P1R0S";

fn active(name: &str) -> String {
    String::from_utf8(
        active_filename_for_tombstone(&cps(name))
            .iter()
            .map(|&c| c as u8)
            .collect(),
    )
    .unwrap()
}

#[test]
fn one_final_newline_is_allowed_by_dollar() {
    assert_eq!(active(&format!("a.{ID}.tombstone.md\n")), "a.md");
    assert!(!is_legacy_tombstone_name(&cps(&format!(
        "a.{ID}.tombstone.md\n"
    ))));
}

#[test]
fn two_final_newlines_reject() {
    let n = format!("a.{ID}.tombstone.md\n\n");
    assert_eq!(active(&n), n);
}

#[test]
fn an_inner_newline_rejects_the_id_form() {
    let n = format!("a\nb.{ID}.tombstone.md");
    assert_eq!(active(&n), format!("a\nb.{ID}.md"));
    assert!(is_legacy_tombstone_name(&cps(&n)));
}

#[test]
fn the_stem_is_greedy_and_may_hold_dots() {
    assert_eq!(active(&format!("a.b.{ID}.tombstone.md")), "a.b.md");
    assert_eq!(active(&format!("..{ID}.tombstone.md")), "..md");
}

#[test]
fn an_empty_stem_is_legacy() {
    assert!(is_legacy_tombstone_name(&cps(&format!(
        ".{ID}.tombstone.md"
    ))));
}

#[test]
fn excluded_crockford_letters_reject() {
    for c in ['I', 'L', 'O', 'U', 'a'] {
        let id = format!("{}{c}", &ID[..25]);
        assert!(
            id_tombstone_stem(&cps(&format!("a.{id}.tombstone.md"))).is_none(),
            "{c}"
        );
    }
    for c in ['H', 'J', 'K', 'M', 'N', 'P', 'T', 'V', 'Z'] {
        let id = format!("{}{c}", &ID[..25]);
        assert!(
            id_tombstone_stem(&cps(&format!("a.{id}.tombstone.md"))).is_some(),
            "{c}"
        );
    }
}

#[test]
fn tombstone_filename_strips_one_md() {
    assert_eq!(
        tombstone_filename(&cps("x.md.md"), &cps("I")),
        cps("x.md.I.tombstone.md")
    );
    assert_eq!(
        tombstone_filename(&cps("x.MD"), &cps("I")),
        cps("x.MD.I.tombstone.md")
    );
}

#[test]
fn has_surrogates_reads_keys_and_depth_but_not_paths() {
    let s = PyValue::Str(vec![0xDC80]);
    assert!(has_surrogates(&PyValue::Dict(vec![(
        s.clone(),
        PyValue::None
    )])));
    assert!(has_surrogates(&PyValue::List(vec![PyValue::Tuple(vec![
        s
    ])])));
    assert!(!has_surrogates(&PyValue::Path(vec![0xDC80])));
    assert!(!has_surrogates(&PyValue::Str(vec![0xD7FF, 0xE000])));
}

#[test]
fn a_surrogate_key_keeps_its_entry_in_the_legacy_mapping() {
    let o = vec![(PyValue::Str(vec![0xDCFF]), PyValue::str("v"))];
    let (legacy, native) = origin_projection(&o).unwrap();
    assert_eq!(legacy, o);
    assert_eq!(native, Some(cps("{\"\\udcff\":\"v\"}")));
}

#[test]
fn encoded_json_sorts_and_escapes() {
    let v = PyValue::Dict(vec![
        (PyValue::str("b"), PyValue::str("\u{e9}")),
        (PyValue::str("a"), PyValue::int(1)),
    ]);
    assert_eq!(
        encoded_json(&v).unwrap(),
        cps("{\"a\":1,\"b\":\"\\u00e9\"}")
    );
}
