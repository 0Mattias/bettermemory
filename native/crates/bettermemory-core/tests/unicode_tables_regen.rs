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

use std::path::PathBuf;
use std::process::Command;

#[test]
fn regenerating_the_unicode_tables_gives_the_same_bytes() {
    let native = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let named = std::env::var_os("BM_REFERENCE_PYTHON").map(PathBuf::from);
    let strict = named.is_some();
    let python = named.unwrap_or_else(|| native.join("../.venv/bin/python"));
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
    if !strict && output.status.code() == Some(2) {
        eprintln!(
            "not checked: {} is not the reference Python ({}); set \
             BM_REFERENCE_PYTHON to a Python 3.13 (Unicode 15.1.0)",
            python.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        );
        return;
    }
    assert!(
        output.status.success(),
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
