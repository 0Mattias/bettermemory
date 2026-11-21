//! Cases a plausible wrong port of `membership_records::records` answers
//! otherwise than the reference does. Each answer was read from the
//! reference (CPython 3.13.13).

use bettermemory_core::capture::location::{to_location, ObjectKey};
use bettermemory_core::capture::{Capture, Manifest, State};
use bettermemory_core::checkout_token::{self, Lineage};
use bettermemory_core::json;
use bettermemory_core::membership_records::records::{
    bounded, found_token, live_key, manifest_from_json, manifest_match, Checkout, View,
};
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::unicode::cps;

const TOKEN: &str = "0123456789ABCDEFGHJKMNPQRS";

fn load(text: &str) -> PyValue {
    json::loads(&cps(text)).expect("JSON")
}

fn entries(text: &str) -> Result<Vec<(String, String)>, PyError> {
    let m = manifest_from_json(&load(text))?.expect("a manifest");
    Ok(m.entries
        .into_iter()
        .map(|(p, s)| (p.iter().map(|&c| char::from_u32(c).unwrap()).collect(), s))
        .collect())
}

fn pair(p: &str, s: &str) -> (String, String) {
    (p.to_owned(), s.to_owned())
}

fn manifest(name: &str, e: &[(&str, &str)]) -> Manifest {
    Manifest {
        name: cps(name),
        entries: e.iter().map(|(p, s)| (cps(p), (*s).to_owned())).collect(),
    }
}

fn checkout(token: Option<&str>, status: &str) -> Checkout {
    Checkout {
        id: cps("k1"),
        project: cps("p1"),
        machine: cps("m1"),
        kind: cps("primary"),
        object_key: Some(ObjectKey {
            device: cps("10"),
            inode: cps("20"),
            birth: None,
        }),
        common_object_key: None,
        git_dir: None,
        token: token.map(cps),
        token_status: cps(status),
        stamps: Vec::new(),
        last_head: None,
        branch: None,
        remotes: Vec::new(),
        manifest: None,
        legacy: false,
        retired: false,
        registered: cps("t0"),
        views: Vec::new(),
        sessions: Vec::new(),
        replaced_by: None,
        ticks: None,
    }
}

fn view(machine: &str, path: &str, order: i64) -> View {
    View {
        checkout: cps("k1"),
        machine: cps(machine),
        path: cps(path),
        first_seen: cps("t0"),
        last_seen: cps("t1"),
        order,
    }
}

#[test]
fn a_dict_of_entries_yields_its_keys_each_split_in_two() {
    assert_eq!(
        entries(r#"{"name":"m","entries":{"a1":"x"}}"#),
        Ok(vec![pair("a", "1")])
    );
}

#[test]
fn a_two_character_element_unpacks_into_its_characters() {
    assert_eq!(
        entries(r#"{"name":"m","entries":["p7"]}"#),
        Ok(vec![pair("p", "7")])
    );
    assert_eq!(
        entries(r#"{"name":"m","entries":["p"]}"#),
        Err(PyError::ValueError)
    );
}

#[test]
fn a_falsy_entries_value_is_no_entries_and_a_truthy_scalar_raises() {
    assert_eq!(entries(r#"{"name":"m","entries":0.0}"#), Ok(vec![]));
    assert_eq!(entries(r#"{"name":"m","entries":""}"#), Ok(vec![]));
    assert_eq!(
        entries(r#"{"name":"m","entries":NaN}"#),
        Err(PyError::TypeError)
    );
    assert_eq!(
        entries(r#"{"name":"m","entries":true}"#),
        Err(PyError::TypeError)
    );
}

#[test]
fn sizes_and_paths_convert_as_python_does() {
    assert_eq!(
        entries(r#"{"name":"m","entries":[["p",-2.9]]}"#),
        Ok(vec![pair("p", "-2")])
    );
    assert_eq!(
        entries(r#"{"name":"m","entries":[["p"," 1_0 "]]}"#),
        Ok(vec![pair("p", "10")])
    );
    assert_eq!(
        entries(r#"{"name":"m","entries":[[true,1]]}"#),
        Ok(vec![pair("True", "1")])
    );
    assert_eq!(
        entries(r#"{"name":"m","entries":[["p",Infinity]]}"#),
        Err(PyError::OverflowError)
    );
    assert_eq!(
        entries(r#"{"name":"m","entries":[["p",NaN]]}"#),
        Err(PyError::ValueError)
    );
}

#[test]
fn the_first_raise_in_element_order_is_the_answer() {
    // The bad size of the first element raises before the second's shape.
    assert_eq!(
        entries(r#"{"name":"m","entries":[["a","x"],5]}"#),
        Err(PyError::ValueError)
    );
    assert_eq!(
        entries(r#"{"name":"m","entries":[5,["a","x"]]}"#),
        Err(PyError::TypeError)
    );
}

#[test]
fn a_non_dict_or_a_name_not_text_is_no_manifest() {
    assert_eq!(manifest_from_json(&load(r#"["name","m"]"#)), Ok(None));
    assert_eq!(
        manifest_from_json(&load(r#"{"name":1,"entries":5}"#)),
        Ok(None)
    );
}

#[test]
fn the_match_reads_sets_and_two_empty_trees_score_nothing() {
    let a = manifest("x", &[("a", "1"), ("a", "1"), ("a", "1")]);
    let b = manifest("x", &[("a", "1"), ("b", "1")]);
    assert_eq!(
        manifest_match(Some(&a), Some(&b)).to_bits(),
        0.5f64.to_bits()
    );
    let e = manifest("x", &[]);
    assert_eq!(
        manifest_match(Some(&e), Some(&e)).to_bits(),
        0.0f64.to_bits()
    );
    let c = manifest("y", &[("a", "1")]);
    assert_eq!(manifest_match(Some(&a), Some(&c)), 0.0);
    let d = manifest("x", &[("a", "1"), ("b", "2"), ("c", "3")]);
    let one = manifest("x", &[("a", "1")]);
    assert_eq!(
        manifest_match(Some(&d), Some(&one)).to_bits(),
        (1.0f64 / 3.0).to_bits()
    );
}

#[test]
fn the_newest_view_is_the_first_of_equal_orders_and_views_on_is_stable() {
    let mut k = checkout(None, "own");
    k.views = vec![
        view("m1", "/a", 2),
        view("m1", "/b", 2),
        view("m2", "/c", 1),
    ];
    assert_eq!(k.newest_view().map(|v| v.path.clone()), Some(cps("/a")));
    let on: Vec<PyStr> = k
        .views_on(&cps("m1"))
        .iter()
        .map(|v| v.path.clone())
        .collect();
    assert_eq!(on, vec![cps("/a"), cps("/b")]);
}

#[test]
fn a_superseded_token_is_refuted_only_by_no_token() {
    let k = checkout(Some(TOKEN), "superseded");
    assert_eq!(k.token_refutes(Some(&cps(""))), Ok(true));
    assert_eq!(k.token_refutes(None), Ok(true));
    assert_eq!(k.token_refutes(Some(&cps("anything"))), Ok(false));
    assert!(k.token_superseded());
    assert!(!checkout(Some(TOKEN), "Superseded").token_superseded());
}

#[test]
fn a_remint_of_the_own_token_does_not_refute_it() {
    let k = checkout(Some(TOKEN), "own");
    let lineage = Lineage {
        token: cps(TOKEN),
        device: cps("10"),
        inode: cps("20"),
    };
    assert_eq!(k.lineage(), Some(lineage.clone()));
    let nonce = cps("0123456789ABC");
    let remint = format!(
        "0123456789ABC{}",
        checkout_token::link(&lineage, &nonce).unwrap()
    );
    assert_eq!(k.token_refutes(Some(&cps(&remint))), Ok(false));
    assert_eq!(k.token_refutes(Some(&cps(TOKEN))), Ok(false));
    assert_eq!(k.token_refutes(None), Ok(true));
    assert_eq!(checkout(Some(""), "own").token_refutes(None), Ok(false));
}

#[test]
fn the_found_token_falls_back_to_the_written_one_unless_superseded() {
    let mut cap = Capture::new(cps("/w"), State::Repo);
    cap.token = Some(Vec::new());
    cap.token_written = Some(cps("W"));
    let loc = to_location(&cap, &cps("m1"), None);
    assert_eq!(found_token(&checkout(None, "own"), &loc), Some(&cps("W")));
    assert_eq!(
        found_token(&checkout(None, "superseded"), &loc),
        Some(&Vec::new())
    );
}

#[test]
fn the_live_key_reads_the_superseded_flag_not_the_project() {
    let v = view("m1", "/p", 1);
    let a = checkout(Some(TOKEN), "own");
    let mut b = a.clone();
    b.project = cps("p2");
    b.token_status = cps("written");
    assert_eq!(live_key(&a, &v), live_key(&b, &v));
    b.token_status = cps("superseded");
    assert_ne!(live_key(&a, &v), live_key(&b, &v));
    assert_ne!(live_key(&a, &v), live_key(&a, &view("m1", "/p/", 1)));
}

#[test]
fn a_reader_that_does_not_say_was_cut_short() {
    assert!(bounded(None));
    assert!(!bounded(Some(false)));
}
