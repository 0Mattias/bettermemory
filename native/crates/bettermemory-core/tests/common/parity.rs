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
        "datetime_edges.jsonl"
        | "json_edges.jsonl"
        | "toml.jsonl"
        | "settings_python.json"
        | "settings_paths_python.json"
        | "membership_lines_python.jsonl"
        | "capture_vocabulary_python.jsonl" => &[Python],
        "lexicon.jsonl"
        | "lexicon_spelling_tables.json"
        | "unicode.jsonl"
        | "unicode_sequences.jsonl"
        | "membership_lines_unicode.jsonl" => &[Unicode],
        "search.jsonl"
        | "search_ranker.jsonl"
        | "search_store.jsonl"
        | "store_files.jsonl"
        | "store_bases.json"
        | "store_admission.json"
        | "store_real.json"
        | "store_real.sqlite" => &[Unicode, Platform],
        "models.jsonl" => &[Pydantic],
        // `str.strip`, `str.lower` and NFKC past ASCII (`gen_origin_remote`).
        "origin_remote_unicode.jsonl" => &[Unicode],
        // A bracketed host: `urlsplit`'s and `ipaddress`'s checks are the
        // release's; some of these URLs also hold text past ASCII.
        "origin_remote_python.jsonl" => &[Python, Unicode],
        // The tokenizer's and the digest's fingerprints read `unicodedata`.
        "store_create.json" => &[Unicode],
        // The tokenizer's fingerprint decides a re-spelling; the digest's
        // rows are its tokens.
        "store_open.jsonl" => &[Unicode],
        "fold.jsonl" => &[Unicode, Pydantic],
        // The index text is the tokenizer's; a refusal's text is the first
        // line of a pydantic ValidationError.
        "store_write.jsonl"
        | "store_tx.jsonl"
        | "store_records.jsonl"
        | "store_records_b.jsonl"
        | "store_records_c.jsonl"
        | "store_records_d.jsonl"
        | "store_records_e.jsonl"
        | "store_rows_written.jsonl"
        | "store_bulk.jsonl" => &[Unicode, Pydantic],
        "models_rows.jsonl" => &[Python, Pydantic],
        // 3.11's realpath walks a link loop otherwise (`gen_fs_realpath`).
        "fs_realpath_python.jsonl" => &[Python],
        // 3.11's strict walk asks the OS where 3.13's raises ENOTDIR at a
        // non-directory followed by `.`, `..` or a separator.
        "fs_realpath_strict_python.jsonl" => &[Python],
        // The same by the strict walk's input shape (`gen_gitdir.gated`).
        "gitdir_read_python.jsonl" => &[Python],
        // A link loop, by the standing rule (`gen_identity.gated`).
        "gitfile_identity_python.jsonl" => &[Python],
        // The strict walk of `_git_common` by input shape (`gen_trust.gated`).
        "gitfile_trust_python.jsonl" => &[Python],
        // `_git_common`'s strict walk, a loop, a record past the JSON
        // decoder's recursion limit, by input (`gen_follow.gated`).
        "gitfile_follow_python.jsonl" => &[Python],
        // A link loop's class name is read as 3.11 and 3.13 read it, but
        // trees holding one are gated by the standing rule.
        "gitconfig_legacy_python.jsonl" => &[Python],
        "models_stores.jsonl" => &[Python, Sqlite, Pydantic],
        // `re`'s `\d` and `int` of a hunk count past ASCII or past 4,300
        // digits (`gen_origin_gitfiles`).
        "origin_gitfiles_leaves_python.jsonl" => &[Python],
        // The restore lock is a directory `flock` on POSIX, a record file
        // and a handle on Windows; the generator runs on POSIX only.
        "store_restore_lock.json" => &[Platform],
        // The rows hold the tokenizer's spelling; the records are built
        // through the models.
        "store_provenance.jsonl" => &[Unicode, Pydantic],
        // A loose header's size past `int`'s 4,300 digits (`gen_gitobjects`).
        "gitobjects_python.jsonl" => &[Python],
        // `int()` and `float()` of a recorded stamp or time read non-ASCII
        // digits and spaces from `unicodedata` (`gen_gitsystem_record`).
        "gitsystem_record.jsonl" => &[Unicode],
        // Records nested past 1,000 levels: whether `json.loads` raises
        // RecursionError is the interpreter's (`gen_gitsystem_record`).
        "gitsystem_record_python.jsonl" => &[Python],
        // `str.strip`, `re`'s `\d`, `int` and `str.upper` of text past
        // ASCII (`gen_gitsystem_answers`).
        "gitsystem_answers_unicode.jsonl" => &[Unicode],
        // 3.11's `ntpath.splitdrive` of a separator then a colon; `int`'s
        // digit limit.
        "gitsystem_answers_python.jsonl" => &[Python],
        // What `repr` keeps unescaped is the Unicode data's `isprintable`
        // (`gen_gitsystem_probe`).
        "gitsystem_probe_names_unicode.jsonl" => &[Unicode],
        // A link or folder checked through a folder that cannot be
        // searched: 3.13's `Path.is_dir` raises, 3.14's answers False.
        "gitsystem_probe_python.jsonl" => &[Python],
        // `str.strip` and `gitversion.parse` of git's text past ASCII
        // (`gen_gitsystem_ask`).
        "gitsystem_ask_unicode.jsonl" => &[Unicode],
        // A version part past `int`'s 4,300 digits.
        "gitsystem_ask_python.jsonl" => &[Python],
        // `shutil.which` before 3.12 always searches the current folder
        // and keeps a PATHEXT entry's trailing dot.
        "gitsystem_which_windows_python.jsonl" => &[Python],
        // A version part past `int`'s 4,300 digits (`gen_gitsystem_sources`).
        "gitsystem_sources_python.jsonl" => &[Python],
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
        // Tests on threads of one process each append a line: the whole
        // line, newline included, goes in one `write_all` to a file opened
        // for append (`writeln!` to an unbuffered file is two writes, the
        // line and then the newline, which other threads' lines can split),
        // and a process-wide lock takes the writers in turn as well.
        static LOG: std::sync::Mutex<()> = std::sync::Mutex::new(());
        let _held = LOG.lock().unwrap_or_else(|e| e.into_inner());
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .expect("BM_PARITY_LOG opens");
        f.write_all(format!("{line}\n").as_bytes())
            .expect("BM_PARITY_LOG takes a line");
    }
}
