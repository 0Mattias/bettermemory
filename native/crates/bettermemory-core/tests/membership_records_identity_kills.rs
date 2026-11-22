//! Cases a plausible wrong port of the portable identity answers
//! otherwise than `membership_records.py` does (CPython 3.13.13).

use bettermemory_core::membership_records::identity::{full_sha, PortableIdentity};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

fn doc(fields: Vec<(&str, PyValue)>) -> PyValue {
    let mut items = vec![(PyValue::str("name"), PyValue::str("p"))];
    items.extend(fields.into_iter().map(|(k, v)| (PyValue::str(k), v)));
    PyValue::Dict(items)
}

fn strs(items: &[String]) -> PyValue {
    PyValue::List(items.iter().map(|s| PyValue::str(s)).collect())
}

fn err(raw: &PyValue) -> String {
    let text = PortableIdentity::from_json(raw).expect_err("a ValueError");
    text.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

fn hex40(i: usize) -> String {
    format!("{i:040x}")
}

#[test]
fn a_bad_witness_is_named_before_bad_file_ids() {
    let raw = doc(vec![
        ("file_ids", strs(&[String::new()])),
        ("witnesses", strs(&["a".repeat(39)])),
    ]);
    assert!(err(&raw).contains("witness 'aaaa"));
}

#[test]
fn bad_remotes_are_named_before_bad_witnesses() {
    let raw = doc(vec![
        ("witnesses", strs(&["x".into()])),
        ("remotes", PyValue::int(1)),
    ]);
    assert_eq!(
        err(&raw),
        "a project identity's 'remotes' is a list of strings"
    );
}

#[test]
fn the_witness_is_named_after_strip_and_lower() {
    let raw = doc(vec![("witnesses", strs(&["\u{130}".repeat(40)]))]);
    let text = err(&raw);
    // Lower makes 80 code points of 40; the slice keeps 72 (36 pairs).
    assert_eq!(text.matches("i\u{307}").count(), 36, "{text}");
    let raw = doc(vec![(
        "witnesses",
        strs(&[format!(" {} ", "A".repeat(39))]),
    )]);
    assert!(err(&raw).contains(&format!("'{}'", "a".repeat(39))));
}

#[test]
fn the_slice_comes_before_the_repr() {
    let raw = doc(vec![("witnesses", strs(&["'".repeat(80)]))]);
    let text = err(&raw);
    assert!(text.contains(&format!("\"{}\"", "'".repeat(72))), "{text}");
}

#[test]
fn duplicates_go_before_the_bound() {
    let mut w: Vec<String> = (0..3).flat_map(|_| (0..3).map(hex40)).collect();
    w.extend((3..70).map(hex40));
    let p = PortableIdentity::from_json(&doc(vec![("witnesses", strs(&w))])).unwrap();
    assert_eq!(p.witnesses.len(), 64);
    assert_eq!(p.witnesses[3], cps(&hex40(3)));
    assert_eq!(p.witnesses[63], cps(&hex40(63)));
}

#[test]
fn every_witness_is_checked_past_the_bound() {
    let mut w: Vec<String> = (0..64).map(hex40).collect();
    w.push("a".repeat(39));
    assert!(err(&doc(vec![("witnesses", strs(&w))])).contains("is not a full commit id"));
}

#[test]
fn local_keys_are_dropped_by_exact_prefix() {
    let r: Vec<String> = ["b", "local:x", "Local:x", "local", "a", "b", "local:"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    let p = PortableIdentity::from_json(&doc(vec![("remotes", strs(&r))])).unwrap();
    let want: Vec<_> = ["Local:x", "a", "b", "local"]
        .iter()
        .map(|s| cps(s))
        .collect();
    assert_eq!(p.remotes, want);
}

#[test]
fn parents_take_any_nonnegative_int_and_no_bool() {
    let big = "1".repeat(500);
    let p = PortableIdentity::from_json(&doc(vec![("parent", PyValue::Int(big.clone()))]));
    assert_eq!(p.unwrap().parent, Some(big));
    for v in [
        PyValue::Bool(false),
        PyValue::int(-1),
        PyValue::Float(0.0),
        PyValue::str("0"),
    ] {
        assert_eq!(
            err(&doc(vec![("parent", v)])),
            "a project identity's parent is a position or null"
        );
    }
}

#[test]
fn flags_are_bools_only() {
    assert_eq!(
        err(&doc(vec![
            ("boundary", PyValue::int(1)),
            ("folder", PyValue::int(0))
        ])),
        "a project identity's 'folder' is true or false"
    );
}

#[test]
fn full_sha_is_forty_or_sixty_four_lowercase_hex() {
    assert!(full_sha(&cps(&"0".repeat(40))));
    assert!(full_sha(&cps(&"f".repeat(64))));
    for n in [39, 41, 63, 65, 104] {
        assert!(!full_sha(&cps(&"a".repeat(n))));
    }
    assert!(!full_sha(&cps(&"A".repeat(40))));
    assert!(!full_sha(&cps(&"g".repeat(40))));
    assert!(!full_sha(&cps(&"\u{ff41}".repeat(40))));
}

#[test]
fn to_json_keeps_the_reference_key_order() {
    let p = PortableIdentity::named(cps("p"));
    let json = p.to_json();
    let PyValue::Dict(items) = &json else {
        panic!()
    };
    let keys: Vec<_> = items.iter().map(|(k, _)| k.clone()).collect();
    let want: Vec<_> = [
        "name",
        "file_ids",
        "witnesses",
        "remotes",
        "folder",
        "boundary",
        "parent",
    ]
    .iter()
    .map(|k| PyValue::str(k))
    .collect();
    assert_eq!(keys, want);
}

#[test]
fn digest_sorts_file_ids_and_remotes_but_not_witnesses() {
    let mut a = PortableIdentity::named(cps("p"));
    a.file_ids = vec![cps("b"), cps("a")];
    a.remotes = vec![cps("y"), cps("x")];
    let mut b = a.clone();
    b.file_ids.reverse();
    b.remotes.reverse();
    assert_eq!(a.digest(), b.digest());
    a.witnesses = vec![cps(&hex40(1)), cps(&hex40(2))];
    b.witnesses = vec![cps(&hex40(2)), cps(&hex40(1))];
    assert_ne!(a.digest(), b.digest());
    // The parent is not part of the digest.
    let mut c = a.clone();
    c.parent = Some("3".into());
    assert_eq!(a.digest(), c.digest());
    assert_eq!(PortableIdentity::named(cps("p")).digest().len(), 24);
}

#[test]
fn repr_doubles_a_backslash_and_lone_surrogates_sort_by_code_point() {
    let raw = doc(vec![("witnesses", strs(&["\\".repeat(80)]))]);
    assert!(err(&raw).contains(&format!("'{}'", "\\".repeat(144))));
    let raw = PyValue::Dict(vec![
        (PyValue::str("name"), PyValue::str("p")),
        (
            PyValue::str("remotes"),
            PyValue::List(vec![
                PyValue::Str(vec![0xFFFD]),
                PyValue::Str(vec![0xD800]),
                PyValue::Str(vec![0xE9]),
                PyValue::Str(vec![0x1F600]),
            ]),
        ),
    ]);
    let p = PortableIdentity::from_json(&raw).unwrap();
    assert_eq!(
        p.remotes,
        vec![vec![0xE9], vec![0xD800], vec![0xFFFD], vec![0x1F600]]
    );
    // `ensure_ascii` writes the lone surrogate as its escape.
    assert_eq!(p.digest().len(), 24);
}

mod outcome_defaults {
    use bettermemory_core::membership_records::outcomes::{
        Declaration, Declared, Handoff, Health, RecallAdmission,
    };
    use bettermemory_core::unicode::cps;

    #[test]
    fn declaration_new_takes_the_reference_defaults() {
        let d = Declaration::new(cps("adopt"), cps("owner"));
        assert_eq!(d.action, cps("adopt"));
        assert_eq!(d.principal, cps("owner"));
        assert!(!d.boundary);
        for f in [
            &d.checkout,
            &d.proposal,
            &d.target,
            &d.file_id,
            &d.name,
            &d.directory,
            &d.record,
            &d.outer_tenant,
            &d.replaced,
            &d.said,
        ] {
            assert!(f.is_none());
        }
    }

    #[test]
    fn declared_new_has_no_remint_checkout() {
        let d = Declared::new(cps("adopt"), None, Some(cps("p")), vec![3, 4]);
        assert_eq!(d.rows, vec![3, 4]);
        assert_eq!(d.project, Some(cps("p")));
        assert!(d.remint_checkout.is_none());
    }

    #[test]
    fn health_new_has_nothing_unresolved() {
        let h = Health::new(vec![], vec![], vec![(cps("s"), 2)], 1, 5, vec![cps("x")]);
        assert_eq!((h.indeterminate, h.deferred), (1, 5));
        assert_eq!(h.suppressed_turns, vec![(cps("s"), 2)]);
        assert!(h.unresolved.is_empty());
    }

    #[test]
    fn handoff_new_has_no_episodes_and_no_label() {
        let h = Handoff::new(Some(cps("s")), cps("latest"));
        assert_eq!(h.session_id, Some(cps("s")));
        assert_eq!(h.rule, cps("latest"));
        assert!(h.episodes.is_empty());
        assert!(h.label.is_none());
    }

    #[test]
    fn recall_admission_defaults_to_empty() {
        let r = RecallAdmission::default();
        assert!(r.delivered.is_empty() && r.suppressed.is_empty());
    }
}
