//! Wide signed counts, rendering limits, and large ordered sections.
use bettermemory_core::{
    cli::membership_health::{health_dict, health_lines},
    membership_records::outcomes::Health,
    pyvalue::PyValue,
    unicode::cps,
};
fn projected(counts: Vec<(Vec<u32>, PyValue)>) -> PyValue {
    health_dict(
        || {
            Ok(Health::new(
                vec![],
                vec![],
                counts,
                PyValue::int(0),
                PyValue::int(0),
                vec![],
            ))
        },
        |_| panic!("no checkouts"),
    )
    .unwrap()
}
#[test]
fn opposite_signs_and_carries_work_beyond_machine_integers() {
    let a = format!("1{}", "0".repeat(300));
    let b = format!("-{}", "9".repeat(300));
    let d = projected(vec![
        (cps("a"), PyValue::Int(a)),
        (cps("b"), PyValue::Int(b)),
    ]);
    assert!(health_lines(&d)
        .unwrap()
        .contains(&cps("Membership: suppressed recall turns (1):")));
}
#[test]
fn an_out_of_range_sum_or_an_individual_addend_fails_at_display() {
    let large = format!("1{}", "0".repeat(4300));
    let canceled = projected(vec![
        (cps("p"), PyValue::Int(large.clone())),
        (cps("n"), PyValue::Int(format!("-{large}"))),
    ]);
    let e = health_lines(&canceled).unwrap_err();
    assert_eq!(e.class, "ValueError");
    assert_eq!(
        e.message,
        cps(bettermemory_core::toml::INT_STR_LIMIT_MESSAGE)
    );
    let carry = projected(vec![
        (cps("p"), PyValue::Int("9".repeat(4300))),
        (cps("q"), PyValue::int(1)),
    ]);
    assert_eq!(health_lines(&carry).unwrap_err(), e);
}
#[test]
fn many_distinct_turns_and_missing_checkouts_remain_ordered() {
    let turns = (0..10000)
        .rev()
        .map(|i| (cps(&format!("K{i:05}")), i64::MAX))
        .collect();
    let ids = (0..5000).map(|i| cps(&format!("K{i:05}"))).collect();
    let mut calls = 0;
    let d = health_dict(
        || Ok(Health::new(vec![], vec![], turns, 0, 0, ids)),
        |_| {
            calls += 1;
            Ok(None)
        },
    )
    .unwrap();
    assert_eq!(calls, 5000);
    let lines = health_lines(&d).unwrap();
    assert_eq!(lines.len(), 15009);
    assert!(lines.contains(&cps(
        "Membership: suppressed recall turns (92233720368547758070000):"
    )));
    assert_eq!(
        lines.last().unwrap(),
        &cps("  K04999, last seen at nowhere")
    );
}
