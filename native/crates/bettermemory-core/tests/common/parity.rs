//! Where the golden files are read from, what made them, and which of
//! their values this machine can be held to (unit PAR).
//!
//! `BM_GOLDEN_DIR` names a directory to read the golden files from in
//! place of the committed `tests/contract/golden`. Such a directory is
//! written by `python -m tests.contract.generate --out DIR --as-found`, and
//! holds a `platform.json` manifest saying what made it: the platform, the
//! Python, its SQLite and its Unicode data. The committed files were made
//! as the `COMMITTED_*` constants say (pinned against `tests/contract/generate.py` by
//! `the_committed_provenance_is_generate_py_s`).
//!
//! The tests sort into four classes (native/README.md, "Parity"):
//!
//! - (i) platform- and version-independent: always compared strictly.
//! - (ii) tied by design to the Python or Unicode version the port
//!   reproduces ([`PORT_PYTHON`], [`PORT_UNICODE`]): strict where the files
//!   were made under those versions; under others a case that differs is
//!   set aside and named, never failed and never hidden.
//! - (iii) floating-point score bits, a property of one platform: strict
//!   where the files were made on this platform; elsewhere a case that
//!   differs is set aside and named.
//! - (os) an expectation the operating system cannot meet, whatever made
//!   the files (Windows cannot remove an open file and has no mode bits):
//!   the test checks what holds there instead and records the case as set
//!   aside. No gate gives this class; a test passes it to [`record`] itself.
//!
//! A gate opens only on the files' recorded provenance, file by file, as
//! `tests/contract/generate.py`'s `*_GATED` sets gate the Python side. With
//! every gate shut a difference fails the test as it always did. With one
//! open, each case is still compared: an equal case counts as compared, a
//! differing one is printed with its difference and the reason and counted
//! as set aside. Every gated test prints one `PARITY` line (and appends it
//! to `BM_PARITY_LOG` when set) with both counts, so nothing set aside is
//! silent; `tools/native_parity.py` reads them.

use std::fmt::Display;
use std::io::Write;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::PathBuf;
use std::sync::OnceLock;

use serde_json::{json, Value};

/// The Python and Unicode versions the port reproduces: its Unicode tables,
/// CPython 3.13.13's datetime parser and `list.sort`, its nesting budget.
pub const PORT_PYTHON: &str = "3.13.13";
pub const PORT_UNICODE: &str = "15.1.0";
/// The pydantic-core whose coercions and errors the record models reproduce.
pub const PORT_PYDANTIC_CORE: &str = "2.50.1";
/// The SQLite the record store fixtures were written with: a store another
/// SQLite writes (its text affinity, its file layout) is a different input.
pub const PORT_SQLITE: &str = "3.50.4";

/// What made the committed files (`generate.py`'s pins).
pub const COMMITTED_PLATFORM: &str = "darwin-arm64";
pub const COMMITTED_PYTHON: &str = "3.13.13";
pub const COMMITTED_SQLITE: &str = "3.50.4";
pub const COMMITTED_UNICODE: &str = "15.1.0";
pub const COMMITTED_PYDANTIC_CORE: &str = "2.50.1";

#[derive(Clone, Debug)]
pub struct Provenance {
    pub platform: String,
    pub python: String,
    pub sqlite: String,
    pub unicode: String,
    pub pydantic_core: String,
    /// "committed" or the manifest's path.
    pub source: String,
}

pub fn committed_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/contract/golden")
}

/// `BM_GOLDEN_DIR`, else the committed directory.
pub fn golden_dir() -> PathBuf {
    match std::env::var_os("BM_GOLDEN_DIR") {
        Some(dir) if !dir.is_empty() => PathBuf::from(dir),
        _ => committed_dir(),
    }
}

pub fn under_test_is_committed() -> bool {
    std::env::var_os("BM_GOLDEN_DIR").is_none_or(|d| d.is_empty())
}

/// What made the files under test.
pub fn provenance() -> &'static Provenance {
    static P: OnceLock<Provenance> = OnceLock::new();
    P.get_or_init(|| {
        if under_test_is_committed() {
            return Provenance {
                platform: COMMITTED_PLATFORM.into(),
                python: COMMITTED_PYTHON.into(),
                sqlite: COMMITTED_SQLITE.into(),
                unicode: COMMITTED_UNICODE.into(),
                pydantic_core: COMMITTED_PYDANTIC_CORE.into(),
                source: "committed".into(),
            };
        }
        let path = golden_dir().join("platform.json");
        let text = std::fs::read_to_string(&path).unwrap_or_else(|e| {
            panic!(
                "BM_GOLDEN_DIR has no manifest at {}: {e}; write the directory with \
                 `python -m tests.contract.generate --out DIR --as-found`",
                path.display()
            )
        });
        let doc: Value = serde_json::from_str(&text).expect("platform.json is JSON");
        let field = |k: &str| {
            doc[k]
                .as_str()
                .unwrap_or_else(|| panic!("platform.json has no {k}"))
                .to_owned()
        };
        Provenance {
            platform: field("platform"),
            python: field("python"),
            sqlite: field("sqlite"),
            unicode: field("unicode"),
            // A manifest from before the record models has no entry.
            pydantic_core: doc["pydantic_core"]
                .as_str()
                .unwrap_or("unknown")
                .to_owned(),
            source: path.display().to_string(),
        }
    })
}

/// This machine as `generate.platform_key()` names it
/// (`sys.platform`-`platform.machine().lower()`); `BM_PLATFORM_KEY`
/// overrides it on both sides, to simulate another platform.
pub fn runner_platform() -> String {
    if let Ok(key) = std::env::var("BM_PLATFORM_KEY") {
        if !key.is_empty() {
            return key;
        }
    }
    let os = match std::env::consts::OS {
        "macos" => "darwin",
        "windows" => "win32",
        other => other,
    };
    let machine = match (std::env::consts::OS, std::env::consts::ARCH) {
        ("windows", "x86_64") => "amd64",
        ("macos", "aarch64") | ("windows", "aarch64") => "arm64",
        (_, arch) => arch,
    };
    format!("{os}-{machine}")
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Gate {
    /// Class (ii): the Python patch release the port reproduces.
    Python,
    /// Class (ii): the Unicode data the port carries.
    Unicode,
    /// Class (iii): floating-point bits of one platform.
    Platform,
    /// Class (ii): the pydantic-core the record models reproduce.
    Pydantic,
    /// Class (ii): the SQLite that wrote the record store fixtures.
    Sqlite,
}

/// The gates of each golden file: the Python side's `*_GATED` sets, by
/// the generator that writes the file. SQLite is not a gate here: the
/// native build links its own SQLite, so a reference SQLite of another
/// version is recorded in the manifest and compared strictly. The one
/// exception is `models_stores.jsonl`, whose stores the reference SQLite
/// writes: their bytes are the input under test.
pub fn gates_of(file: &str) -> &'static [Gate] {
    use Gate::*;
    match file {
        "datetime_edges.jsonl" | "json_edges.jsonl" => &[Python],
        "lexicon.jsonl"
        | "lexicon_spelling_tables.json"
        | "unicode.jsonl"
        | "unicode_sequences.jsonl" => &[Unicode],
        "search.jsonl"
        | "search_ranker.jsonl"
        | "search_store.jsonl"
        | "store_files.jsonl"
        | "store_bases.json"
        | "store_admission.json"
        | "store_real.json"
        | "store_real.sqlite" => &[Unicode, Platform],
        "models.jsonl" => &[Pydantic],
        "fold.jsonl" => &[Unicode, Pydantic],
        "models_rows.jsonl" => &[Python, Pydantic],
        "models_stores.jsonl" => &[Python, Sqlite, Pydantic],
        // The restore lock is a directory `flock` on POSIX, a record file
        // and a handle on Windows; the generator runs on POSIX only.
        "store_restore_lock.json" => &[Platform],
        _ => &[],
    }
}

/// The class a file's gates put a test in.
pub fn class_of(gates: &[Gate]) -> &'static str {
    if gates.contains(&Gate::Platform) {
        "iii"
    } else if gates.is_empty() {
        "i"
    } else {
        "ii"
    }
}

/// Why each open gate of `gates` is open, or nothing when every one is shut.
pub fn open_gates(gates: &[Gate]) -> Vec<String> {
    let p = provenance();
    let mut out = Vec::new();
    for gate in gates {
        match gate {
            Gate::Python if p.python != PORT_PYTHON => out.push(format!(
                "files from Python {}, the port reproduces {PORT_PYTHON}",
                p.python
            )),
            Gate::Unicode if p.unicode != PORT_UNICODE => out.push(format!(
                "files from Unicode {}, the port carries {PORT_UNICODE}",
                p.unicode
            )),
            Gate::Platform if p.platform != runner_platform() => out.push(format!(
                "files from platform {}, this is {}",
                p.platform,
                runner_platform()
            )),
            Gate::Pydantic if p.pydantic_core != PORT_PYDANTIC_CORE => out.push(format!(
                "files from pydantic-core {}, the port reproduces {PORT_PYDANTIC_CORE}",
                p.pydantic_core
            )),
            Gate::Sqlite if p.sqlite != PORT_SQLITE => out.push(format!(
                "files from SQLite {}, the store fixtures are {PORT_SQLITE}'s",
                p.sqlite
            )),
            _ => {}
        }
    }
    out
}

/// The tally of one test against one golden file.
pub struct Ledger {
    test: String,
    file: String,
    class: &'static str,
    why: Vec<String>,
    compared: usize,
    aside: Vec<String>,
}

impl Ledger {
    pub fn new(test: &str, file: &str) -> Ledger {
        let gates = gates_of(file);
        Ledger {
            test: test.into(),
            file: file.into(),
            class: class_of(gates),
            why: open_gates(gates),
            compared: 0,
            aside: Vec::new(),
        }
    }

    /// True when every gate is shut: a difference fails.
    pub fn strict(&self) -> bool {
        self.why.is_empty()
    }

    /// Records a case already compared: true when the caller must fail it
    /// (it differs and every gate is shut).
    pub fn differs_fatally(&mut self, label: impl Display, same: bool) -> bool {
        if same {
            self.compared += 1;
            return false;
        }
        if self.strict() {
            return true;
        }
        self.set_aside(label.to_string());
        false
    }

    /// Runs one case's assertions. With every gate shut a failing
    /// assertion fails the test; with one open it is printed (the panic
    /// message is the difference) and the case is set aside.
    pub fn case(&mut self, label: impl Display, check: impl FnOnce()) {
        if self.strict() {
            check();
            self.compared += 1;
            return;
        }
        match catch_unwind(AssertUnwindSafe(check)) {
            Ok(()) => self.compared += 1,
            Err(_) => self.set_aside(label.to_string()),
        }
    }

    fn set_aside(&mut self, label: String) {
        eprintln!(
            "NOT COMPARABLE HERE {} {}: {label} differs ({})",
            self.test,
            self.file,
            self.why.join("; ")
        );
        self.aside.push(label);
    }

    /// For a test that gathers a description of each differing cell:
    /// `total` cells were compared and `failures` of them differ. With
    /// every gate shut any failure fails the test.
    pub fn settle(mut self, total: usize, failures: &[String]) {
        assert!(
            !self.strict() || failures.is_empty(),
            "{}: {} failures:\n{}",
            self.file,
            failures.len(),
            failures.join("\n")
        );
        for failure in failures {
            self.set_aside(failure.clone());
        }
        self.compared = total - failures.len();
        record(
            &self.test,
            &self.file,
            self.class,
            self.compared,
            &self.aside,
            &self.why,
        );
    }

    pub fn set_aside_count(&self) -> usize {
        self.aside.len()
    }

    /// Asserts every case of the file was looked at and logs the tally.
    pub fn finish(self, in_file: usize) {
        let looked = self.compared + self.aside.len();
        assert!(looked > 0, "{}: no case checked", self.file);
        assert_eq!(
            looked, in_file,
            "{}: checked {looked} of {in_file} cases",
            self.file
        );
        record(
            &self.test,
            &self.file,
            self.class,
            self.compared,
            &self.aside,
            &self.why,
        );
    }
}

/// `major.minor` of the Python that made the files under test (the
/// headers of the version-gated files record it).
pub fn python_minor() -> String {
    provenance()
        .python
        .splitn(3, '.')
        .take(2)
        .collect::<Vec<_>>()
        .join(".")
}

/// One `PARITY` line: stderr, and `BM_PARITY_LOG` when set.
pub fn record(
    test: &str,
    file: &str,
    class: &str,
    compared: usize,
    aside: &[String],
    why: &[String],
) {
    let p = provenance();
    let line = json!({
        "test": test,
        "file": file,
        "class": class,
        "compared": compared,
        "set_aside": aside.len(),
        "set_aside_cases": aside.iter().take(20).collect::<Vec<_>>(),
        "why": if aside.is_empty() { Value::Null } else { json!(why.join("; ")) },
        "gates_open": why,
        "files": p.source,
    });
    eprintln!(
        "PARITY {test} [{file}, class {class}]: {compared} compared, {} set aside{}",
        aside.len(),
        if aside.is_empty() {
            String::new()
        } else {
            format!(" ({})", why.join("; "))
        }
    );
    if let Some(path) = std::env::var_os("BM_PARITY_LOG") {
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .expect("BM_PARITY_LOG opens");
        writeln!(f, "{line}").expect("BM_PARITY_LOG takes a line");
    }
}
