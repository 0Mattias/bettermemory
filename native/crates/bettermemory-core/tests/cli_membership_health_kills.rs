//! Regressions for ordering and the precise guarded region of a health lookup.
use bettermemory_core::{
    cli::membership_health::{health_dict, health_lines, where_path, CheckoutError},
    membership_records::outcomes::Health,
    pyvalue::PyValue,
    recall_prompt::PyException,
    unicode::cps,
};
fn empty() -> Health {
    Health::new(vec![], vec![], vec![], 0, 0, vec![])
}
fn err(class: &str) -> PyException {
    PyException {
        class: class.into(),
        message: vec![0xdcff],
    }
}
#[test]
fn only_null_skips_lookup_and_errors_stop_at_the_originating_read() {
    let mut calls = vec![];
    let mut lookup = |id: &[u32]| {
        calls.push(id.to_vec());
        Err(CheckoutError::Missing)
    };
    assert_eq!(where_path(None, &mut lookup), Ok(None));
    assert_eq!(where_path(Some(&[]), &mut lookup), Ok(None));
    assert_eq!(calls, vec![Vec::<u32>::new()]);
    assert_eq!(
        where_path(Some(&[]), &mut |_| Err(CheckoutError::Raised(err(
            "KeyError"
        )))),
        Err(err("KeyError"))
    );
    let r = health_dict::<i64>(
        || Err(err("KeyError")),
        |_| panic!("lookup after a failed health read"),
    );
    assert_eq!(r, Err(err("KeyError")));
}
#[test]
fn repeats_are_resolved_separately_and_projection_only_sorts_turns() {
    let mut h = empty();
    h.without_live_view = vec![cps("K"), cps("K")];
    h.suppressed_turns = vec![(vec![0xdcff], 1), (cps("a"), 2)];
    let mut i = 0;
    let got = health_dict(
        || Ok(h),
        |_| {
            i += 1;
            Ok(Some(cps(&i.to_string())))
        },
    )
    .unwrap();
    let PyValue::Dict(top) = &got else {
        unreachable!()
    };
    let keys: Vec<_> = top.iter().map(|(k, _)| k.clone()).collect();
    assert_eq!(
        keys,
        [
            "open_proposals",
            "provisional_projects",
            "unresolved_projects",
            "suppressed_recall_turns",
            "indeterminate_captures",
            "deferred_captures",
            "checkouts_without_live_view"
        ]
        .map(PyValue::str)
    );
    let lines = health_lines(&got).unwrap();
    assert!(lines.contains(&cps("  K, last seen at \"1\"")));
    assert!(lines.contains(&cps("  K, last seen at \"2\"")));
    let PyValue::Dict(turns) = &top[3].1 else {
        unreachable!()
    };
    assert_eq!(turns[0].0, PyValue::str("a"));
}
#[test]
fn strict_quoting_of_absent_unresolved_path_preserves_the_reference_error() {
    let mut h = empty();
    h.unresolved.push(
        bettermemory_core::membership_records::outcomes::UnresolvedProject {
            project: cps("P"),
            name: cps("n"),
            state: cps("u"),
            checkout: None,
            path: None,
            memories: 0,
            episodes: 0,
        },
    );
    let d = health_dict(|| Ok(h), |_| panic!("unresolved path is stored directly")).unwrap();
    let e = health_lines(&d).unwrap_err();
    assert_eq!(e.class, "TypeError");
    assert_eq!(e.message, cps("'NoneType' object is not iterable"));
}
