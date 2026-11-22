//! Hostile input to `settle_interrupted`: many turns, many audited rows,
//! a prompt id nested deep in a row, clocks at the start of the
//! calendar. No panic, and the work grows linearly.

mod settle_int;
mod timing;

use serde_json::{json, Value};
use settle_int::run;

fn turns(n: usize) -> Value {
    Value::Array(
        (0..n)
            .map(|i| json!([format!("P{i}"), "how do we deploy", null, null]))
            .collect(),
    )
}

fn audited(n: usize) -> Value {
    Value::Array(
        (0..n)
            .map(|i| json!({"kind": "turn_audited", "session": "S", "prompt_id": format!("P{}", i * 2)}))
            .collect(),
    )
}

#[test]
fn many_turns_and_rows() {
    let (o, _) = run(
        "int",
        &json!({"turns": turns(20_000), "ev": audited(10_000)}),
    );
    assert!(matches!(o, bettermemory_core::pyvalue::PyValue::List(ref x) if x.len() == 10_000));
}

#[test]
fn a_prompt_id_nested_deep() {
    let mut v = json!("P1");
    for _ in 0..1_000 {
        v = json!([v]);
    }
    let ev = json!([{"kind": "turn_audited", "session": "S", "prompt_id": v}]);
    let (o, _) = run("int", &json!({"turns": turns(1), "ev": ev}));
    assert!(matches!(o, bettermemory_core::pyvalue::PyValue::List(ref x) if x.len() == 1));
}

#[test]
fn clocks_at_the_start_of_the_calendar() {
    for now in [
        "0001-01-01T00:00:00+00:00",
        "0001-01-01T23:59:59+00:00",
        "0001-01-02T00:00:00+00:00",
    ] {
        let _ = run("int", &json!({"turns": turns(1), "now": now}));
    }
    let _ = run(
        "int",
        &json!({"turns": turns(1), "now": "9999-12-31T23:59:59+00:00"}),
    );
}

#[test]
fn settlement_grows_linearly() {
    let small = json!({"turns": turns(500), "ev": audited(500)});
    let large = json!({"turns": turns(5_000), "ev": audited(5_000)});
    timing::grows_within(
        "settle_interrupted",
        timing::TENFOLD,
        &mut || timing::once(|| run("int", &small)),
        &mut || timing::once(|| run("int", &large)),
    );
}
