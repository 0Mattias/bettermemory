//! Every answer of `settings.json` (`tests/contract/gen_config.py`)
//! against the native loader and reports: the catalogue and the fixed
//! settings, each key read alone from each probe (the value, the refusal
//! text, whether a warning was logged, and what `config check` finds in a
//! file holding that line), `config path --json` under each environment,
//! and for each case `config check --json` and `config show --json` (the
//! printed text byte for byte and the exit code, or the exception that
//! escaped; each command with fresh one-shot guards) and the audit's
//! config findings.
//!
//! The world is the generator's, as data: every path is `<scratch>/...`,
//! the working directory `<scratch>/alpha`, the home `<scratch>/home`, and
//! the location variables point into `<scratch>`. The reference's own
//! paths are written back as `<scratch>` (`_sandbox.Scrubber`), so the
//! same inputs give the same text whatever platform made the file.

mod common;

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

use bettermemory_core::config::report;
use bettermemory_core::config::{
    self, Ctx, Env, Flavour, Fs, Guards, Locations, ManagedSource, OsFailure, Setting, Source,
    World,
};
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::toml::{Int, Table, Value as Toml};
use common::golden_path;
use common::parity::Ledger;
use serde_json::{json, Value};

const ROOT: &str = "<scratch>";

/// The generator's world: directories and files under `<scratch>`.
struct FakeFs {
    dirs: BTreeSet<String>,
    files: RefCell<BTreeMap<String, Vec<u8>>>,
}

impl FakeFs {
    fn new(extra_dirs: &[String]) -> FakeFs {
        let mut dirs: BTreeSet<String> = [
            "",
            "/mem",
            "/keys",
            "/state",
            "/home",
            "/home/.config",
            "/home/.local/state",
            "/home/.local/share",
            "/home/.cache",
            "/home/AppData/Roaming",
            "/home/AppData/Local",
            "/alpha",
            "/user",
            "/managed",
        ]
        .iter()
        .map(|d| format!("{ROOT}{d}"))
        .collect();
        // The file system's root, above the run's directory.
        dirs.insert("/".to_owned());
        dirs.extend(extra_dirs.iter().cloned());
        FakeFs {
            dirs,
            files: RefCell::new(BTreeMap::new()),
        }
    }

    fn put(&self, path: &str, bytes: Vec<u8>) {
        self.files.borrow_mut().insert(path.to_owned(), bytes);
    }
}

fn normalize(path: &str) -> String {
    let mut parts: Vec<&str> = Vec::new();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            p => parts.push(p),
        }
    }
    let joined = parts.join("/");
    if path.starts_with('/') {
        format!("/{joined}")
    } else {
        joined
    }
}

impl FakeFs {
    /// `path` made absolute against the working directory.
    fn absolute(&self, path: &str) -> String {
        if path.starts_with(ROOT) || path.starts_with('/') {
            path.to_owned()
        } else {
            format!("{ROOT}/alpha/{path}")
        }
    }

    /// Where the OS's walk of `path` lands: each `.` and `..` is taken
    /// only from a directory that exists, as a kernel's lookup takes it
    /// (`<scratch>/a/../keys` does not exist when `<scratch>/a` does not).
    fn walk(&self, path: &str) -> Option<String> {
        if path.contains('\0') {
            return None;
        }
        let absolute = self.absolute(path);
        let (root, rest) = absolute
            .strip_prefix(ROOT)
            .map_or(("", absolute.as_str()), |rest| (ROOT, rest));
        let mut here: Vec<&str> = Vec::new();
        let spelled = |here: &[&str]| match format!("{root}{}", joined(here)) {
            p if p.is_empty() => "/".to_owned(),
            p => p,
        };
        let dir = |here: &[&str]| self.dirs.contains(&spelled(here));
        for part in rest.split('/').filter(|p| !p.is_empty()) {
            match part {
                "." => {
                    if !dir(&here) {
                        return None;
                    }
                }
                ".." => {
                    if !dir(&here) {
                        return None;
                    }
                    here.pop();
                }
                p => {
                    if !here.is_empty() && !dir(&here) {
                        return None;
                    }
                    here.push(p);
                }
            }
        }
        Some(spelled(&here))
    }
}

fn joined(parts: &[&str]) -> String {
    parts.iter().map(|p| format!("/{p}")).collect()
}

impl Fs for FakeFs {
    fn read(&self, path: &str) -> Result<Vec<u8>, Option<OsFailure>> {
        self.files.borrow().get(path).cloned().ok_or(None)
    }
    fn present(&self, path: &str) -> Result<bool, config::Raised> {
        if path.contains('\0') {
            return Err(config::nul_in_path("lstat"));
        }
        Ok(self
            .walk(path)
            .is_some_and(|p| self.files.borrow().contains_key(&p) || self.dirs.contains(&p)))
    }
    fn is_symlink(&self, _: &str) -> bool {
        false
    }
    fn is_dir(&self, path: &str) -> bool {
        self.walk(path).is_some_and(|p| self.dirs.contains(&p))
    }
    /// `os.path.realpath`, non-strict: no symlinks in this world, so `..`
    /// folds lexically, whether or not what it leaves exists.
    fn resolve(&self, path: &str) -> Result<String, config::Raised> {
        if path.contains('\0') {
            return Err(config::nul_in_path("lstat"));
        }
        Ok(normalize(&self.absolute(path)))
    }
    /// The world has one user, whose home is `<scratch>/home`; no other
    /// user's name expands (`~name` raises, as it does for a name the
    /// system does not know). A `~name` that exists cannot be expressed.
    fn expanduser(&self, path: &str) -> Result<String, config::Raised> {
        if path == "~" {
            Ok(format!("{ROOT}/home"))
        } else if let Some(rest) = path.strip_prefix("~/") {
            Ok(format!("{ROOT}/home/{rest}"))
        } else if path.starts_with('~') {
            Err(config::no_home())
        } else {
            Ok(path.to_owned())
        }
    }
    fn join(&self, path: &str, name: &str) -> String {
        format!("{path}/{name}")
    }
    fn home(&self) -> Option<String> {
        Some(format!("{ROOT}/home"))
    }
    fn cwd(&self) -> Option<String> {
        Some(format!("{ROOT}/alpha"))
    }
    fn under(&self, resolved: &str, prefix: &str) -> bool {
        resolved.starts_with(&format!("{prefix}/"))
    }
}

fn user_path() -> String {
    format!("{ROOT}/user/config.toml")
}

fn managed_path() -> String {
    format!("{ROOT}/managed/managed.toml")
}

fn locations() -> Locations {
    Locations {
        user_config: user_path(),
        managed: ManagedSource::File(managed_path()),
        default_keys_dir: format!("{ROOT}/home/.config/bettermemory/keys"),
        default_state_dir: format!("{ROOT}/home/.local/state/bettermemory"),
        system_dirs: [
            "/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys",
        ]
        .iter()
        .map(|d| ((*d).to_owned(), (*d).to_owned()))
        .collect(),
        flavour: Flavour::Posix,
    }
}

fn world_env() -> Env {
    [
        (config::ENV_DIR_OVERRIDE, "/mem"),
        (config::KEYS_DIR_ENV, "/keys"),
        (config::STATE_DIR_ENV, "/state"),
    ]
    .iter()
    .map(|(k, v)| ((*k).to_owned(), format!("{ROOT}{v}")))
    .collect()
}

fn toml_of(value: &Value) -> Toml {
    match value {
        Value::Bool(b) => Toml::Boolean(*b),
        Value::Number(n) if n.is_i64() => {
            Toml::Integer(Int::from_i128(i128::from(n.as_i64().unwrap())))
        }
        Value::Number(n) => Toml::Float(n.as_f64().unwrap()),
        Value::String(s) => Toml::String(s.clone()),
        Value::Array(items) => Toml::Array(items.iter().map(toml_of).collect()),
        Value::Object(map) => {
            let mut t = Table::new();
            for (k, v) in map {
                t.insert(k.clone(), toml_of(v));
            }
            Toml::Table(t)
        }
        Value::Null => panic!("no TOML null"),
    }
}

fn py_json(value: &PyValue) -> Value {
    let options = DumpOptions {
        default_str: true,
        ..DumpOptions::DEFAULT
    };
    let text = json::utf8_text(&json::dumps(value, options).expect("dumps"));
    serde_json::from_str(&text).expect("JSON")
}

fn setting_json(value: &Setting) -> Value {
    py_json(&value.to_py().expect("a printable setting"))
}

fn fixture(file: &str) -> Value {
    let text = std::fs::read_to_string(golden_path(file)).expect(file);
    serde_json::from_str(&text).expect("JSON")
}

fn settings() -> Value {
    fixture("settings.json")
}

#[test]
fn settings_catalogue_and_fixed() {
    let doc = settings();
    let catalogue = doc["catalogue"].as_array().unwrap();
    let mut ledger = Ledger::new("golden_settings_catalogue", "settings.json");
    assert_eq!(catalogue.len(), config::CATALOGUE.len());
    for (entry, (section, key, kind)) in catalogue.iter().zip(config::CATALOGUE) {
        ledger.case(format!("{section}.{key}"), || {
            assert_eq!(entry["section"], section);
            assert_eq!(entry["key"], key);
            assert_eq!(entry["type"], kind);
            assert_eq!(
                entry["default"],
                setting_json(&config::default_of(section, key))
            );
            // Pinned alone through a managed file, a value other than the
            // default is in force.
            let other = match config::default_of(section, key) {
                Setting::Bool(b) => Toml::Boolean(!b),
                Setting::Int(_) => Toml::Integer(Int::from_i128(7400)),
                Setting::None => Toml::String("/srv/bettermemory".into()),
                Setting::Str(_) => Toml::String("everywhere".into()),
                Setting::List(_) if key == "forbid_acknowledge" => {
                    Toml::Array(vec![Toml::String("credential".into())])
                }
                Setting::List(_) => Toml::Array(vec![Toml::String("tools".into())]),
            };
            let fs = FakeFs::new(&[]);
            fs.put(
                &managed_path(),
                format!(
                    "[{section}]\n{key} = {}\n",
                    config::toml_spelling(&other).unwrap()
                )
                .into_bytes(),
            );
            let (env, locations) = (world_env(), locations());
            let world = World {
                fs: &fs,
                env: &env,
                locations: &locations,
            };
            let mut guards = Guards::new();
            let mut ctx = Ctx::new(&mut guards);
            let loaded = config::load_config(&world, &user_path(), &mut ctx).unwrap();
            let pinned = loaded.is_managed(&format!("{section}.{key}"))
                && Some(other.clone()) == loaded.get(section, key).to_raw();
            assert_eq!(entry["managed_may_pin"], pinned);
        });
    }
    let fixed = doc["fixed"].as_array().unwrap();
    assert_eq!(fixed.len(), config::FIXED_SETTINGS.len());
    for (entry, (section, key, value)) in fixed.iter().zip(config::FIXED_SETTINGS) {
        ledger.case(format!("fixed {key}"), || {
            assert_eq!(entry["section"], section);
            assert_eq!(entry["key"], key);
            let want = report::toml_to_py(&value.to_raw()).unwrap();
            assert_eq!(entry["value"], py_json(&want));
            assert_eq!(entry["limit"], json!(config::fixed_limit(section, key)));
        });
    }
    let names: Vec<&str> = doc["environment"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e["name"].as_str().unwrap())
        .collect();
    ledger.case("environment", || assert_eq!(names, config::ENVIRONMENT));
    ledger.finish(catalogue.len() + fixed.len() + 1);
}

/// A world with one config.toml and one managed file (None: no file).
fn run_world(
    user: Option<Vec<u8>>,
    managed: Option<Vec<u8>>,
    extra_dirs: &[String],
    env: &Env,
    f: impl FnOnce(&World),
) {
    let fs = FakeFs::new(extra_dirs);
    if let Some(bytes) = user {
        fs.put(&user_path(), bytes);
    }
    if let Some(bytes) = managed {
        fs.put(&managed_path(), bytes);
    }
    let locations = locations();
    let world = World {
        fs: &fs,
        env,
        locations: &locations,
    };
    f(&world);
}

fn check_codes(text: &str) -> Value {
    let doc: Value = serde_json::from_str(text).expect("check prints JSON");
    Value::Array(
        doc["findings"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| json!([f["level"], f["code"]]))
            .collect(),
    )
}

#[test]
fn settings_coercion() {
    let doc = settings();
    let probes = doc["probes"].as_array().unwrap();
    let mut failures = Vec::new();
    let mut total = 0;
    for entry in doc["coercion"].as_array().unwrap() {
        let section = entry["section"].as_str().unwrap();
        let key = entry["key"].as_str().unwrap();
        let reads = entry["reads"].as_array().unwrap();
        let mut last_refused = false;
        for (probe, want) in probes.iter().zip(reads) {
            total += 1;
            let raw = toml_of(probe);
            let mut inner = Table::new();
            inner.insert(key.to_owned(), raw.clone());
            let mut data = Table::new();
            data.insert(section.to_owned(), Toml::Table(inner));
            let mut guards = Guards::new();
            let mut ctx = Ctx::new(&mut guards);
            let source = Source {
                text: "config.toml",
                resolved: "config.toml".into(),
                is_file: true,
            };
            let mut got = match config::config_from_data(&mut data, &source, &mut ctx) {
                Ok(c) => json!({"value": setting_json(c.get(section, key))}),
                Err(e) => json!({"refused": e.message}),
            };
            last_refused = got.get("refused").is_some();
            got["warned"] = json!(!ctx.log.is_empty());
            let line = format!(
                "[{section}]\n{key} = {}\n",
                config::toml_spelling(&raw).unwrap()
            );
            let env = world_env();
            run_world(Some(line.into_bytes()), None, &[], &env, |world| {
                let mut guards = Guards::new();
                let (text, _) = report::check(world, &mut guards).expect("check runs");
                got["check"] = check_codes(&text);
            });
            if &got != want {
                failures.push(format!(
                    "{section}.{key} <- {probe}: native {got}, reference {want}"
                ));
            }
        }
        let on_bad = if last_refused { "refuse" } else { "default" };
        total += 1;
        if entry["on_bad_value"] != on_bad {
            failures.push(format!("{section}.{key} on_bad_value: native {on_bad}"));
        }
    }
    Ledger::new("golden_settings_coercion", "settings.json").settle(total, &failures);
}

fn bytes_of(case: &Value, name: &str) -> Option<Vec<u8>> {
    if let Some(text) = case[name].as_str() {
        return Some(text.as_bytes().to_vec());
    }
    case[format!("{name}_hex")].as_str().map(common::unhex)
}

/// One command's result against the file's record of it: the printed
/// text byte for byte and the exit code, or the exception that escaped.
fn compare_run(
    label: &str,
    got: Result<(String, i32), config::Raised>,
    want: &Value,
    failures: &mut Vec<String>,
) {
    let got_doc = match &got {
        Ok((text, code)) => json!({"exit_code": code, "text": text}),
        Err(raised) => json!({"raised": raised.class, "message": raised.message}),
    };
    if &got_doc != want {
        failures.push(format!("{label}: native {got_doc}\nreference {want}"));
    }
}

/// Each of `doc`'s `cases` (check, show and the audit) against the
/// native reports: the descriptions of the cells that differ.
fn run_cases(doc: &Value, failures: &mut Vec<String>) -> usize {
    let cases = doc["cases"].as_array().unwrap();
    let env = world_env();
    for case in cases {
        let name = case["case"].as_str().unwrap();
        run_world(
            bytes_of(case, "config_toml"),
            bytes_of(case, "managed_toml"),
            &[],
            &env,
            |world| {
                // Each command as a CLI process of its own runs it, with
                // fresh one-shot guards, as the generator runs them.
                compare_run(
                    &format!("{name} check"),
                    report::check(world, &mut Guards::new()),
                    &case["check"],
                    failures,
                );
                compare_run(
                    &format!("{name} show"),
                    report::show(world, &mut Guards::new()),
                    &case["show"],
                    failures,
                );
                let audit = match report::audit_settings(world, &mut Guards::new()) {
                    Ok(Ok(findings)) => py_json(&findings),
                    Ok(Err(message)) => json!({"does_not_load": message}),
                    Err(raised) => json!({"raised": raised.class, "message": raised.message}),
                };
                if audit != case["audit_config_findings"] {
                    failures.push(format!(
                        "{name} audit: native {audit}\nreference {}",
                        case["audit_config_findings"]
                    ));
                }
            },
        );
    }
    cases.len() * 3
}

/// Each of `doc`'s `config_paths` against `report::path` in its world.
#[cfg(unix)]
fn run_path_cases(doc: &Value, failures: &mut Vec<String>) -> usize {
    let cases = doc["config_paths"].as_array().unwrap();
    for case in cases {
        let name = case["case"].as_str().unwrap();
        let mut env = world_env();
        for (k, v) in case["env"].as_object().unwrap() {
            env.insert(k.clone(), v.as_str().unwrap().to_owned());
        }
        let dirs: Vec<String> = case["mkdir"]
            .as_array()
            .unwrap()
            .iter()
            .map(|d| format!("{ROOT}/{}", d.as_str().unwrap()))
            .collect();
        run_world(
            bytes_of(case, "config_toml"),
            bytes_of(case, "managed_toml"),
            &dirs,
            &env,
            |world| {
                let mut guards = Guards::new();
                let want: Value = case
                    .as_object()
                    .unwrap()
                    .iter()
                    .filter(|(k, _)| {
                        matches!(k.as_str(), "exit_code" | "text" | "raised" | "message")
                    })
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect::<serde_json::Map<_, _>>()
                    .into();
                compare_run(name, report::path(world, &mut guards), &want, failures);
            },
        );
    }
    cases.len()
}

#[test]
fn settings_cases() {
    let mut failures = Vec::new();
    let total = run_cases(&settings(), &mut failures);
    Ledger::new("golden_settings_cases", "settings.json").settle(total, &failures);
}

/// The cases the interpreter words (Python-gated).
#[test]
fn settings_python_cases() {
    let mut failures = Vec::new();
    let total = run_cases(&fixture("settings_python.json"), &mut failures);
    Ledger::new("golden_settings_python_cases", "settings_python.json").settle(total, &failures);
}

/// The cases that expand and resolve paths by POSIX rules (POSIX only:
/// `settings_paths_not_posix.rs` records the gap elsewhere).
#[cfg(unix)]
#[test]
fn settings_paths() {
    let doc = fixture("settings_paths.json");
    let mut failures = Vec::new();
    let total = run_path_cases(&doc, &mut failures) + run_cases(&doc, &mut failures);
    Ledger::new("golden_settings_paths", "settings_paths.json").settle(total, &failures);
}

/// A NUL in a path: POSIX only and Python-gated.
#[cfg(unix)]
#[test]
fn settings_paths_python() {
    let doc = fixture("settings_paths_python.json");
    let mut failures = Vec::new();
    let total = run_path_cases(&doc, &mut failures) + run_cases(&doc, &mut failures);
    Ledger::new("golden_settings_paths_python", "settings_paths_python.json")
        .settle(total, &failures);
}

#[test]
fn settings_config_path() {
    let doc = settings();
    let mut failures = Vec::new();
    let env = world_env();
    run_world(None, None, &[], &env, |world| {
        let mut guards = Guards::new();
        compare_run(
            "config_path",
            report::path(world, &mut guards),
            &doc["config_path"],
            &mut failures,
        );
    });
    Ledger::new("golden_settings_config_path", "settings.json").settle(1, &failures);
}

/// `config::pure_path` against `str(PurePosixPath(text))` and
/// `str(PureWindowsPath(text))` over `settings_python.json`'s `pure_paths` (the
/// Windows spelling is recorded with `/` for each backslash).
#[test]
fn settings_pure_paths() {
    let doc = fixture("settings_python.json");
    let rows = doc["pure_paths"].as_array().unwrap();
    let mut failures = Vec::new();
    for row in rows {
        let text = row[0].as_str().unwrap();
        let posix = config::pure_path(text, Flavour::Posix);
        if posix != row[1].as_str().unwrap() {
            failures.push(format!(
                "{text:?} posix: native {posix:?}, reference {}",
                row[1]
            ));
        }
        let windows = config::pure_path(text, Flavour::Windows).replace('\\', "/");
        if windows != row[2].as_str().unwrap() {
            failures.push(format!(
                "{text:?} windows: native {windows:?}, reference {}",
                row[2]
            ));
        }
    }
    Ledger::new("golden_settings_pure_paths", "settings_python.json")
        .settle(rows.len() * 2, &failures);
}
