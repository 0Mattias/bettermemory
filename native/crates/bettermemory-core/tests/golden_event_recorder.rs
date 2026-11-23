//! Every case of `event_recorder.jsonl` (not gated) against the native
//! `Recorder`: the store calls, the warnings, what is raised, `records`,
//! `session_for_request` and `worktree_for_request` of each row.

mod common;
mod event_recorder_run;

#[test]
fn every_row() {
    let lines = common::jsonl("event_recorder.jsonl");
    let (header, rows) = lines.split_first().unwrap();
    let mut checked = 0;
    for row in rows {
        assert_eq!(event_recorder_run::run(row), row["r"], "{row}");
        checked += 1;
    }
    common::assert_count(
        "event_recorder.jsonl",
        checked,
        header["count"].as_u64().unwrap() as usize,
    );
}

/// `event_recorder_python.jsonl` (gated on the Python release): a field
/// that collides with a keyword the recorder passes, and CPython's text
/// for it. Under another release a differing case is set aside.
#[test]
fn python_every_row() {
    let name = "event_recorder_python.jsonl";
    let lines = common::jsonl(name);
    let ledger = common::parity::Ledger::new("golden_event_recorder", name);
    let (header, rows) = lines.split_first().unwrap();
    assert_eq!(header["count"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let mut failures = Vec::new();
    for (i, row) in rows.iter().enumerate() {
        let got = event_recorder_run::run(row);
        if got != row["r"] {
            failures.push(format!("#{i}: native {got}\n reference {}", row["r"]));
        }
    }
    ledger.settle(rows.len(), &failures);
}
