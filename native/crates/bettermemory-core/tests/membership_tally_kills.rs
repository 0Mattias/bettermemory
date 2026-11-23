//! Specific ordering, failure and witness decisions from the committed real-model cases.
mod common;
mod membership_tally_cases;

use bettermemory_core::pyvalue::PyValue;
use common::membership_rows::{enc, field, lines, list, name, same};
use membership_tally_cases::run;

fn cases_matching(
    mut test: impl FnMut(&PyValue) -> bool,
    mut inspect: impl FnMut(&PyValue, PyValue, PyValue),
) -> usize {
    let all = lines("membership_tally.jsonl");
    let mut count = 0;
    for row in &all[1..] {
        if test(row) {
            let (got, calls) = run(&all[0], row);
            inspect(row, got, calls.expect("a complete read trace"));
            count += 1;
        }
    }
    count
}
#[test]
fn multi_exclude_precedes_history_and_preserves_remote_order() {
    let n = cases_matching(
        |c| c.get("expect_holds").is_some(),
        |c, got, calls| {
            assert_eq!(&list(&got)[4], field(c, "expect_dropped"));
            let holds: Vec<_> = list(&calls)
                .iter()
                .filter(|call| list(call)[0] == PyValue::str("holds"))
                .collect();
            assert_eq!(holds.len(), 1);
            assert_eq!(&list(holds[0])[1], field(c, "expect_holds"));
        },
    );
    assert_eq!(n, 4);
}
#[test]
fn rewritten_traversal_keeps_first_project_and_reads_null_fingerprint() {
    let n = cases_matching(
        |c| c.get("expect_dropped").is_some() && c.get("expect_holds").is_none(),
        |c, got, calls| {
            assert_eq!(&list(&got)[4], field(c, "expect_dropped"));
            let history: Vec<_> = list(&calls)
                .iter()
                .filter_map(|call| {
                    let v = list(call);
                    (v[0] == PyValue::str("fingerprint")).then(|| name(&v[1]))
                })
                .collect();
            assert_eq!(history, ["h0", "h1", "h2"]);
        },
    );
    assert_eq!(n, 1);
}
#[test]
fn each_fallible_reader_preserves_error_and_stops() {
    let mut seen = std::collections::BTreeSet::new();
    let n = cases_matching(
        |c| c.get("fail_read").is_some(),
        |c, got, calls| {
            let kind = name(field(c, "fail_read"));
            assert!(seen.insert(kind.clone()));
            assert_eq!(&enc(&got), field(c, "encoded_o"));
            let last = list(list(&calls).last().unwrap());
            assert_eq!(
                name(&last[0]),
                if kind == "holds_objects" {
                    "objects"
                } else {
                    &kind
                }
            );
        },
    );
    assert_eq!(n, 6);
}
#[test]
fn hash_seed_order_changes_real_boundary_output() {
    let mut by_seed = std::collections::BTreeMap::<String, Vec<Vec<String>>>::new();
    let n = cases_matching(
        |c| c.get("seed").is_some(),
        |c, got, calls| {
            assert!(same(&enc(&got), field(c, "encoded_o")) && same(&calls, field(c, "calls")));
            let dropped = list(&list(&got)[4]).iter().map(name).collect();
            by_seed
                .entry(name(field(c, "seed")))
                .or_default()
                .push(dropped);
        },
    );
    assert_eq!(n, 4);
    assert_ne!(by_seed["1"], by_seed["2"]);
}
#[test]
fn equal_keys_and_foreign_token_carriers_gate_path_rekey() {
    let mut counts = std::collections::BTreeSet::new();
    let n = cases_matching(
        |c| c.get("expect_rekey").is_some(),
        |c, got, _| {
            assert_eq!(&list(&got)[2], field(c, "expect_rekey"));
            counts.insert(list(&list(&got)[2]).len());
        },
    );
    assert_eq!(n, 5);
    assert_eq!(counts, std::collections::BTreeSet::from([0, 1]));
}
