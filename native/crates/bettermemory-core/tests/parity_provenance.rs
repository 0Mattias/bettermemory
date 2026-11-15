//! What the golden files under test were made with (unit PAR): the
//! committed set's provenance in `common::parity` is `generate.py`'s pins,
//! and the set under test is named in the log of every run.

mod common;

use common::parity::{self, Gate};

fn pin(source: &str, name: &str) -> String {
    let line = source
        .lines()
        .find(|l| l.starts_with(&format!("{name} = \"")))
        .unwrap_or_else(|| panic!("generate.py has no {name}"));
    line.split('"').nth(1).expect("a quoted value").to_owned()
}

#[test]
fn the_committed_provenance_is_generate_py_s() {
    let source = std::fs::read_to_string(parity::committed_dir().join("../generate.py"))
        .expect("tests/contract/generate.py");
    assert_eq!(pin(&source, "PLATFORM"), parity::COMMITTED_PLATFORM);
    assert_eq!(pin(&source, "PYTHON_VERSION"), parity::COMMITTED_PYTHON);
    assert_eq!(pin(&source, "SQLITE_VERSION"), parity::COMMITTED_SQLITE);
    assert_eq!(pin(&source, "UNIDATA_VERSION"), parity::COMMITTED_UNICODE);
    // The port reproduces the versions the committed files were made under.
    assert_eq!(parity::PORT_PYTHON, parity::COMMITTED_PYTHON);
    assert_eq!(parity::PORT_UNICODE, parity::COMMITTED_UNICODE);
    assert_eq!(
        parity::PORT_UNICODE,
        bettermemory_core::unicode::UNIDATA_VERSION
    );
}

#[test]
fn the_files_under_test_are_named() {
    let p = parity::provenance();
    let open = |gates: &[Gate]| parity::open_gates(gates);
    eprintln!(
        "PARITY files under test: {} (platform {}, Python {}, SQLite {}, Unicode {}); \
         this machine {}; open gates: platform {:?}, python {:?}, unicode {:?}",
        parity::golden_dir().display(),
        p.platform,
        p.python,
        p.sqlite,
        p.unicode,
        parity::runner_platform(),
        open(&[Gate::Platform]),
        open(&[Gate::Python]),
        open(&[Gate::Unicode]),
    );
    if !parity::under_test_is_committed() {
        for name in ["search_ranker.jsonl", "store_files.jsonl", "lexicon.jsonl"] {
            assert!(
                parity::golden_dir().join(name).is_file(),
                "BM_GOLDEN_DIR lacks {name}"
            );
        }
    }
}
