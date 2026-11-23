//! Large supported collections and codepoint-preserving witness output.
mod common;
#[path = "common/membership_shared_heal.rs"]
mod heal;
use bettermemory_core::{
    membership_records::records::Specific,
    membership_resolve::tally::{strength, tally, unordered},
    pyvalue::PyValue,
    unicode::cps,
};
use common::membership_views::location;
use std::collections::BTreeSet;

#[test]
fn large_remote_set_and_exclusion_collection_are_read_once() {
    let mut model = heal::model(&PyValue::None);
    let mut loc = location(&PyValue::None);
    loc.remote_keys = (0..20_000).map(|k| cps(&format!("remote-{k}"))).collect();
    let excluded: Vec<_> = (0..20_000).map(|k| cps(&format!("project-{k}"))).collect();
    let result = tally(&mut model, &loc, None, None, &excluded).unwrap();
    assert!(result.cands.is_empty());
    let state = model.store().inner.0.borrow();
    assert_eq!(state.calls.get("rows"), Some(&1));
    assert_eq!(state.depth, 0);
}
#[test]
fn long_unknown_witnesses_and_surrogate_names_are_preserved() {
    let mut model = heal::model(&PyValue::None);
    let loc = location(&PyValue::None);
    let code = vec![0xdcff; 200_000];
    assert_eq!(
        strength(
            &mut model,
            &loc,
            &[],
            &[code.clone(), code.clone(), cps("C")]
        )
        .unwrap(),
        1
    );
    let spec = Specific {
        best: Vec::new(),
        held: -3,
        top: BTreeSet::new(),
        bounded: true,
    };
    let result = unordered(&spec, &[code.clone(), vec![0x10ffff]]);
    assert!(result.starts_with(&code));
    assert!(result.ends_with(&cps("within the walk's bound (at most 512 commits read)")));
    assert!(model.store().inner.0.borrow().log.is_empty());
}
