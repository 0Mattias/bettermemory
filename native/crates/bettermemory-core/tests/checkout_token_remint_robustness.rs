//! Hostile input for `remint`: a very long git directory, lineage fields
//! of a megabyte, a claim read that never ends, and a run of stale break
//! marks. No panic in debug or release; time grows with the input in
//! proportion.

mod ct_remint_script;
mod timing;

use bettermemory_core::checkout_token::write::WriteStatus;
use ct_remint_script::{run, RScript};
use serde_json::{json, Value};

fn status(row: &Value) -> WriteStatus {
    let script = RScript::new(row);
    run(row, &script).expect("an answer").status
}

#[test]
fn a_very_long_git_dir_is_reminted_in_linear_time() {
    let go = |n: usize| {
        let git = format!("/{}", "a/".repeat(n));
        assert_eq!(
            status(&json!({"git": git, "rep": null})),
            WriteStatus::Written
        );
    };
    go(100_000);
    timing::grows_within(
        "long git dir",
        timing::TENFOLD,
        &mut || timing::once(|| go(2_000)),
        &mut || timing::once(|| go(20_000)),
    );
}

#[test]
fn lineage_fields_of_a_megabyte_are_linked_in_linear_time() {
    let go = |n: usize| {
        let field = "7".repeat(n);
        let row = json!({"rep": ["0123456789ABCDEFGHJKMNPQRS", field, field]});
        assert_eq!(status(&row), WriteStatus::Written);
    };
    go(1 << 20);
    timing::grows_within(
        "long lineage",
        timing::TENFOLD,
        &mut || timing::once(|| go(50_000)),
        &mut || timing::once(|| go(500_000)),
    );
}

#[test]
fn a_run_of_stale_marks_is_passed_in_linear_time() {
    let go = |n: usize| {
        let links: Vec<Value> = vec![json!("EEXIST"); 13];
        let mut creates = vec![json!({}), json!({})];
        creates.extend((0..n).map(|_| json!({"open": "EEXIST"})));
        let marks: Vec<Value> = (0..n).map(|_| json!(0.0)).collect();
        let reads: Vec<Value> = (0..12)
            .map(|_| json!({"reads": [{"t": "HELDHELDHELD"}]}))
            .collect();
        let row = json!({"links": links, "creates": creates, "marks": marks, "reads": reads});
        let script = RScript::new(&row);
        let answer = run(&row, &script).expect("an answer");
        assert_eq!(answer.status, WriteStatus::SkippedContended);
    };
    go(3_000);
    timing::grows_within(
        "stale marks",
        timing::TENFOLD,
        &mut || timing::once(|| go(300)),
        &mut || timing::once(|| go(3_000)),
    );
}

#[test]
fn clocks_at_the_ends_of_the_doubles_do_not_panic() {
    for mtime in [f64::MAX, f64::MIN, f64::MIN_POSITIVE, -0.0, 1e308] {
        let row = json!({"mtime": mtime, "links": vec![json!("EEXIST"); 13],
            "creates": [{}, {}, {"open": "EEXIST"}], "marks": [-1e308],
            "reads": vec![json!({"reads": [{"t": "N"}]}); 12]});
        let script = RScript::new(&row);
        let _ = run(&row, &script);
    }
}
