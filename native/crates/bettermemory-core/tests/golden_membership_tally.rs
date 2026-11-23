//! Every outcome and full read trace of the Python membership tally.
mod common;
mod membership_tally_cases;
use common::{
    membership_rows::{enc, field, int, lines, same},
    parity::Ledger,
};
use membership_tally_cases::run;
#[test]
fn membership_tally_matches_reference() {
    let file = "membership_tally.jsonl";
    let data = lines(file);
    let header = &data[0];
    let mut ledger = Ledger::new("membership_tally_matches_reference", file);
    let mut count = 0;
    for (i, case) in data.iter().skip(1).enumerate() {
        let (got, calls) = run(header, case);
        let (got, want) = match case.get("encoded_o") {
            Some(want) => (enc(&got), want),
            None => (got, field(case, "o")),
        };
        assert!(
            !ledger.differs_fatally(format!("row {i} result"), same(&got, want)),
            "row {i} result: {got:?} vs {want:?}"
        );
        count += 1;
        if let Some(calls) = calls {
            let want = field(case, "calls");
            assert!(
                !ledger.differs_fatally(format!("row {i} calls"), same(&calls, want)),
                "row {i} calls: {calls:?} vs {want:?}"
            );
            count += 1;
        }
    }
    assert_eq!(data.len() - 1, int(field(header, "rows")) as usize);
    ledger.finish(count);
}
