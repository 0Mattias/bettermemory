//! The committed `src/unicode/tables.rs` is what the generator writes:
//! runs `native/tools/gen_unicode_tables.py --check` under the reference
//! Python (Unicode 15.1.0). The interpreter is `BM_REFERENCE_PYTHON`, else
//! the repository's `.venv/bin/python`.
//!
//! With `BM_REFERENCE_PYTHON` set the check is strict: any failure fails
//! the test. Without it, a repository `.venv` of another Unicode version
//! (the generator's exit code 2) cannot show where the tables come from,
//! so the test says so and checks nothing, as the Python golden tests skip
//! a file they cannot compare; a missing interpreter still fails.
//!
//! The one exception to strictness (unit PAR): when the golden files under
//! test (`BM_GOLDEN_DIR`) were themselves made under another Unicode, the
//! named interpreter is that Python, which cannot regenerate 15.1.0's
//! tables. That is class (ii), tied to the pinned version: the test prints
//! a PARITY line with the check set aside and why, and does not fail.

mod common;

use common::parity;

use std::path::PathBuf;
use std::process::Command;

#[test]
fn regenerating_the_unicode_tables_gives_the_same_bytes() {
    let native = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let named = std::env::var_os("BM_REFERENCE_PYTHON").map(PathBuf::from);
    let strict = named.is_some();
    // A uv or venv environment: bin/python, or Scripts\python.exe on Windows.
    let python = named.unwrap_or_else(|| {
        let posix = native.join("../.venv/bin/python");
        let windows = native.join("../.venv/Scripts/python.exe");
        if !posix.exists() && windows.exists() {
            windows
        } else {
            posix
        }
    });
    assert!(
        python.exists(),
        "no reference Python at {}: set BM_REFERENCE_PYTHON to Python 3.13 (Unicode 15.1.0)",
        python.display()
    );
    let output = Command::new(&python)
        .arg(native.join("tools/gen_unicode_tables.py"))
        .arg("--check")
        .output()
        .expect("the generator runs");
    let other_unicode = parity::provenance().unicode != parity::PORT_UNICODE;
    if strict && other_unicode && output.status.code() == Some(2) {
        let why = format!(
            "{}; {}",
            parity::open_gates(&[parity::Gate::Unicode]).join("; "),
            String::from_utf8_lossy(&output.stderr).trim()
        );
        eprintln!("NOT COMPARABLE HERE regenerating_the_unicode_tables: {why}");
        parity::record(
            "regenerating_the_unicode_tables_gives_the_same_bytes",
            "src/unicode/tables.rs, src/unicode/xid_tables.rs",
            "ii",
            0,
            &["tables.rs".to_owned(), "xid_tables.rs".to_owned()],
            &[why],
        );
        return;
    }
    if !strict && output.status.code() == Some(2) {
        eprintln!(
            "not checked: {} is not the reference Python ({}); set \
             BM_REFERENCE_PYTHON to a Python 3.13 (Unicode 15.1.0)",
            python.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        );
        parity::record(
            "regenerating_the_unicode_tables_gives_the_same_bytes",
            "src/unicode/tables.rs, src/unicode/xid_tables.rs",
            "ii",
            0,
            &["tables.rs".to_owned(), "xid_tables.rs".to_owned()],
            &[format!("{} is not the reference Python", python.display())],
        );
        return;
    }
    parity::record(
        "regenerating_the_unicode_tables_gives_the_same_bytes",
        "src/unicode/tables.rs, src/unicode/xid_tables.rs",
        "ii",
        usize::from(output.status.success()),
        &[],
        &[],
    );
    assert!(
        output.status.success(),
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
