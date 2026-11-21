//! Every case of `repository_history.jsonl` against
//! `repository_history::RepositoryHistory`: a scripted object reader and a
//! scripted git, each step's answer and git calls compared.

mod common;

use common::jsonl;
use common::parity::Ledger;
use common::repository_history::run;

#[test]
fn repository_history_every_case() {
    let name = "repository_history.jsonl";
    let mut lines = jsonl(name);
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    let mut ledger = Ledger::new("golden_repository_history", name);
    for case in &lines {
        let label = case["name"].as_str().unwrap().to_owned();
        ledger.case(&label, || {
            let got = run(case);
            let want = case["out"].as_array().unwrap();
            assert_eq!(got.len(), want.len(), "{label}");
            for (i, (g, w)) in got.iter().zip(want).enumerate() {
                assert_eq!(g, w, "{label} step {i}");
            }
        });
    }
    ledger.finish(lines.len());
}
