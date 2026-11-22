//! Hostile input to `membership_records::identity`: very many and very
//! long witnesses, deep nesting, a parent of many digits. No panic in
//! debug or release, and the time grows in proportion to the input.

mod timing;

use bettermemory_core::membership_records::identity::PortableIdentity;
use bettermemory_core::pyvalue::PyValue;

fn doc(n: usize) -> PyValue {
    let w = (0..n)
        .map(|i| PyValue::str(&format!("{:040x}", i % 1000)))
        .collect();
    let f = (0..n)
        .map(|i| PyValue::str(&format!("f{}", n - i)))
        .collect();
    PyValue::Dict(vec![
        (PyValue::str("name"), PyValue::str("p")),
        (PyValue::str("witnesses"), PyValue::List(w)),
        (PyValue::str("file_ids"), PyValue::List(f)),
    ])
}

#[test]
fn many_witnesses_read_in_near_linear_time() {
    let (small, large) = (doc(10_000), doc(100_000));
    timing::grows_within(
        "from_json",
        timing::TENFOLD,
        &mut || timing::once(|| PortableIdentity::from_json(&small)),
        &mut || timing::once(|| PortableIdentity::from_json(&large)),
    );
    let p = PortableIdentity::from_json(&large).unwrap();
    assert_eq!((p.witnesses.len(), p.file_ids.len()), (64, 100_000));
    assert_eq!(p.digest().len(), 24);
}

#[test]
fn a_long_bad_witness_is_named_in_72() {
    let raw = PyValue::Dict(vec![
        (PyValue::str("name"), PyValue::str("p")),
        (
            PyValue::str("witnesses"),
            PyValue::List(vec![PyValue::str(&"\u{130}".repeat(1_000_000))]),
        ),
    ]);
    let text = PortableIdentity::from_json(&raw).unwrap_err();
    assert!(text.len() < 200);
}

#[test]
fn deep_values_are_refused_without_recursion() {
    let mut v = PyValue::str("p");
    for _ in 0..100_000 {
        v = PyValue::List(vec![v]);
    }
    for key in [
        "name",
        "parent",
        "witnesses",
        "remotes",
        "file_ids",
        "folder",
    ] {
        let raw = PyValue::Dict(vec![
            (PyValue::str("name"), PyValue::str("p")),
            (PyValue::str(key), v.clone()),
        ]);
        assert!(PortableIdentity::from_json(&raw).is_err(), "{key}");
    }
    assert!(PortableIdentity::from_json(&v).is_err());
}

#[test]
fn a_parent_of_many_digits_is_kept_as_text() {
    let big = "9".repeat(100_000);
    let raw = PyValue::Dict(vec![
        (PyValue::str("name"), PyValue::str("p")),
        (PyValue::str("parent"), PyValue::Int(big.clone())),
    ]);
    assert_eq!(PortableIdentity::from_json(&raw).unwrap().parent, Some(big));
}
