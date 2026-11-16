//! `bettermemory config check|show|path --json` (`cli/config_cmd.py`) and
//! the config findings of `security audit` (`posture._settings`).
//!
//! Each command returns the text the CLI writes to stdout,
//! `json.dumps(document, indent=2, ensure_ascii=True, default=str)` and a
//! line feed, with its exit code; Err is an exception the reference lets
//! escape (the CLI then prints a traceback and exits 1).

use std::cmp::Ordering;

use super::difflib;
use super::*;
use crate::json::{self, DumpOptions};
use crate::pyvalue::PyValue;
use crate::toml::{Table, Value};
use crate::unicode;

pub const ERROR: &str = "error";
pub const WARNING: &str = "warning";
pub const NOTE: &str = "note";
const LEVELS: [&str; 3] = [ERROR, WARNING, NOTE];
pub const USER: &str = "user";
pub const MANAGED: &str = "managed";
const PORT_MAX: i128 = 65535;
const FIRST_UNPRIVILEGED_PORT: i128 = 1024;

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn opt(text: Option<&str>) -> PyValue {
    text.map_or(PyValue::None, s)
}

fn dict(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(items.into_iter().map(|(k, v)| (s(k), v)).collect())
}

/// The CLI's `_emit` for `--json`.
pub fn emit(document: &PyValue) -> Result<String, Raised> {
    let options = DumpOptions {
        sort_keys: false,
        ensure_ascii: true,
        item_separator: ",",
        key_separator: ": ",
        indent: Some(2),
        default_str: true,
    };
    let text = json::dumps(document, options).map_err(|e| Raised {
        class: e.name(),
        message: String::new(),
    })?;
    Ok(json::utf8_text(&text) + "\n")
}

/// A parsed value as `json.dumps(default=str)` meets it.
pub fn toml_to_py(value: &Value) -> Result<PyValue, Raised> {
    Ok(match value {
        Value::String(t) => s(t),
        Value::Integer(i) => PyValue::Int(i.to_decimal().map_err(Raised::value)?),
        Value::Float(f) => PyValue::Float(*f),
        Value::Boolean(b) => PyValue::Bool(*b),
        Value::DateTime(dt) => PyValue::DateTime(*dt),
        Value::Date(_) | Value::Time(_) => {
            s(&toml::py_str_datetime(value).expect("a date or time"))
        }
        Value::Array(items) => {
            PyValue::List(items.iter().map(toml_to_py).collect::<Result<_, _>>()?)
        }
        Value::Table(t) => {
            let mut out = Vec::new();
            for (k, v) in t.iter() {
                out.push((s(k), toml_to_py(v)?));
            }
            PyValue::Dict(out)
        }
    })
}

/// `config_cmd._shown`: a value as config.toml spells it.
fn shown(value: &Value) -> Result<String, Raised> {
    toml_spelling(value)
}

fn shown_setting(value: &Setting) -> Result<String, Raised> {
    match value.to_raw() {
        Some(raw) => toml_spelling(&raw),
        None => Ok("None".into()),
    }
}

/// One of the two sources (`config_cmd.ConfigFile`).
#[derive(Clone, Debug)]
pub struct ConfigFile {
    pub role: &'static str,
    pub path: String,
    pub exists: bool,
    pub data: Option<Table>,
    pub error: Option<String>,
    /// The registry value rather than a file.
    pub registry: bool,
}

impl ConfigFile {
    fn to_py(&self) -> PyValue {
        dict(vec![
            ("role", s(self.role)),
            ("path", s(&self.path)),
            ("exists", PyValue::Bool(self.exists)),
            ("parses", PyValue::Bool(self.data.is_some())),
            ("error", opt(self.error.as_deref())),
            ("note", PyValue::None),
        ])
    }

    fn source<'a>(&'a self, fs: &dyn Fs) -> Source<'a> {
        if self.registry {
            Source::registry()
        } else {
            Source::file(fs, &self.path)
        }
    }
}

/// `config_cmd._read`.
fn read_file(fs: &dyn Fs, role: &'static str, path: &str) -> Result<ConfigFile, Raised> {
    let mut file = ConfigFile {
        role,
        path: path.to_owned(),
        exists: false,
        data: None,
        error: None,
        registry: false,
    };
    if !fs.present(path)? {
        return Ok(file);
    }
    file.exists = true;
    match read_config_file(fs, path) {
        Ok(data) => file.data = Some(data),
        Err(FileError::Raised(raised)) if raised.class == "ValueError" => {
            file.error = Some(raised.message)
        }
        Err(FileError::Raised(raised)) => return Err(raised),
        Err(FileError::NotFound) => {
            let reason = if fs.is_symlink(path) {
                "it is a symlink to a file that does not exist".to_owned()
            } else {
                "No such file or directory".to_owned()
            };
            file.error = Some(format!("{path} could not be read: {reason}"));
        }
        Err(FileError::Os(failure)) => {
            file.error = Some(format!("{path} could not be read: {}", failure.strerror));
        }
    }
    Ok(file)
}

/// `config_cmd._read_managed`.
fn read_managed_file(world: &World) -> Result<ConfigFile, Raised> {
    match &world.locations.managed {
        ManagedSource::File(path) => read_file(world.fs, MANAGED, path),
        ManagedSource::Registry(read) => {
            let mut file = ConfigFile {
                role: MANAGED,
                path: MANAGED_REGISTRY.to_owned(),
                exists: false,
                data: None,
                error: None,
                registry: true,
            };
            match read {
                Err(message) => {
                    file.exists = true;
                    file.error = Some(message.clone());
                }
                Ok(None) => {}
                Ok(Some(text)) => {
                    file.exists = true;
                    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
                    match parse_managed_text(text, MANAGED_REGISTRY) {
                        Ok(data) => file.data = Some(data),
                        Err(raised) if raised.class == "ValueError" => {
                            file.error = Some(raised.message)
                        }
                        Err(raised) => return Err(raised),
                    }
                }
            }
            Ok(file)
        }
    }
}

/// `config_cmd.read_files`.
pub fn read_files(world: &World) -> Result<(ConfigFile, ConfigFile), Raised> {
    Ok((
        read_file(world.fs, USER, &world.locations.user_config)?,
        read_managed_file(world)?,
    ))
}

/// `config_cmd.raw_settings`: (section, key) -> the value as written, in
/// catalogue order.
fn raw_settings(data: Option<&Table>) -> Vec<((&'static str, &'static str), Value)> {
    let mut found = Vec::new();
    let Some(data) = data else {
        return found;
    };
    for section in SECTIONS {
        let Some(Value::Table(table)) = data.get(section) else {
            continue;
        };
        for key in section_keys(section) {
            if let Some(v) = table.get(key) {
                found.push(((section, key), v.clone()));
            }
        }
    }
    found
}

fn lookup<'a>(raw: &'a [((&str, &str), Value)], section: &str, key: &str) -> Option<&'a Value> {
    raw.iter()
        .find(|((s, k), _)| *s == section && *k == key)
        .map(|(_, v)| v)
}

/// The effective config, loaded without its first-run write, and what it
/// logged.
fn load(world: &World, guards: &mut Guards) -> (Result<Config, Raised>, Vec<String>) {
    let mut ctx = Ctx::new(guards);
    let result = load_config(world, &world.locations.user_config, &mut ctx);
    (result, ctx.log)
}

/// One finding of `check`.
#[derive(Clone, Debug)]
pub struct Finding {
    pub level: &'static str,
    pub code: &'static str,
    pub file: Option<&'static str>,
    pub section: Option<String>,
    pub key: Option<String>,
    pub message: String,
    pub suggestion: Option<String>,
}

impl Finding {
    fn new(
        level: &'static str,
        code: &'static str,
        file: Option<&'static str>,
        section: Option<&str>,
        key: Option<&str>,
        message: String,
        suggestion: Option<String>,
    ) -> Finding {
        Finding {
            level,
            code,
            file,
            section: section.map(str::to_owned),
            key: key.map(str::to_owned),
            message,
            suggestion,
        }
    }

    fn to_py(&self) -> PyValue {
        dict(vec![
            ("level", s(self.level)),
            ("code", s(self.code)),
            ("file", opt(self.file)),
            ("section", opt(self.section.as_deref())),
            ("key", opt(self.key.as_deref())),
            ("message", s(&self.message)),
            ("suggestion", opt(self.suggestion.as_deref())),
        ])
    }
}

/// `config_cmd._name`.
fn name(text: &str) -> String {
    if !text.is_empty()
        && text
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
    {
        text.to_owned()
    } else {
        dumps_ascii(text)
    }
}

/// `config_cmd._close`.
fn close(word: &str, candidates: &[String], cutoff: f64) -> Option<String> {
    let mut lowered: Vec<(String, String)> = Vec::new();
    for candidate in candidates {
        let low = py_lower(candidate);
        match lowered.iter_mut().find(|(l, _)| *l == low) {
            Some(slot) => slot.1 = candidate.clone(),
            None => lowered.push((low, candidate.clone())),
        }
    }
    let options: Vec<Vec<u32>> = lowered.iter().map(|(l, _)| unicode::cps(l)).collect();
    let word = unicode::cps(&py_lower(word));
    difflib::get_close_match(&word, &options, cutoff).map(|i| lowered[i].1.clone())
}

fn sections_owned() -> Vec<String> {
    SECTIONS.iter().map(|s| (*s).to_owned()).collect()
}

fn unknown_section(role: &'static str, section: &str) -> Finding {
    let guess = close(section, &sections_owned(), 0.6);
    Finding::new(
        WARNING,
        "unknown_section",
        Some(role),
        Some(section),
        None,
        format!(
            "[{}] is not a section bettermemory reads; every line in it is ignored.",
            name(section)
        ),
        guess.map(|g| format!("did you mean [{g}]?")),
    )
}

fn guess_elsewhere(key: &str, skip: Option<&str>) -> Option<String> {
    for section in SECTIONS {
        if Some(section) != skip && section_keys(section).contains(&key) {
            return Some(format!("[{section}] {key}"));
        }
    }
    let mut pairs: Vec<(String, String)> = Vec::new();
    for section in SECTIONS {
        if Some(section) == skip {
            continue;
        }
        for k in section_keys(section) {
            let label = format!("[{section}] {k}");
            match pairs.iter_mut().find(|(p, _)| p == k) {
                Some(slot) => slot.1 = label,
                None => pairs.push((k.to_owned(), label)),
            }
        }
    }
    let names: Vec<String> = pairs.iter().map(|(k, _)| k.clone()).collect();
    let guess = close(key, &names, 0.8)?;
    pairs
        .into_iter()
        .find(|(k, _)| *k == guess)
        .map(|(_, label)| label)
}

fn unknown_key(role: &'static str, section: Option<&str>, key: &str) -> Finding {
    let (place, suggestion) = match section {
        None => (
            format!("`{}`, outside any section,", name(key)),
            guess_elsewhere(key, None)
                .map(|g| format!("did you mean {g}? Put the line under its section header.")),
        ),
        Some(section) => {
            let keys: Vec<String> = section_keys(section)
                .iter()
                .map(|k| (*k).to_owned())
                .collect();
            let suggestion = match close(key, &keys, 0.6) {
                Some(same) => Some(format!("did you mean {same}?")),
                None => guess_elsewhere(key, Some(section))
                    .map(|g| format!("did you mean {g}? Move the line under that section.")),
            };
            (format!("[{section}] {}", name(key)), suggestion)
        }
    };
    Finding::new(
        WARNING,
        "unknown_key",
        Some(role),
        section,
        Some(key),
        format!("{place} is not a setting bettermemory reads; the line is ignored."),
        suggestion,
    )
}

fn not_a_table(role: &'static str, section: &str, value: &Value) -> Result<Finding, Raised> {
    let outcome = if role == USER && SCALAR_TOLERATED_SECTIONS.contains(&section) {
        "the line is ignored and the section's defaults are in force."
    } else {
        "the config does not load."
    };
    let n = name(section);
    Ok(Finding::new(
        ERROR,
        "not_a_table",
        Some(role),
        Some(section),
        None,
        format!(
            "`{n}` = {} must be a [{n}] section table; {outcome}",
            shown(value)?
        ),
        Some(format!(
            "write the settings under a [{n}] header, one per line"
        )),
    ))
}

fn is_bool_spelling(value: &Value) -> bool {
    match value {
        Value::Boolean(_) => true,
        Value::String(t) => {
            let t = py_lower(py_strip(t));
            ["true", "1", "yes", "on", "false", "0", "no", "off", ""].contains(&t.as_str())
        }
        _ => false,
    }
}

/// `config_cmd._fell_back`.
fn fell_back(section: &str, key: &str, value: &Value) -> bool {
    if (section, key) == ("behavior", "prompt_recall") {
        if matches!(value, Value::Boolean(_)) {
            return false;
        }
        return !match value {
            Value::String(t) => {
                PROMPT_RECALL_MODES.contains(&py_lower(py_strip(t)).as_str())
                    || is_bool_spelling(value)
            }
            _ => false,
        };
    }
    if matches!(default_of(section, key), Setting::Bool(_)) {
        return !is_bool_spelling(value);
    }
    false
}

/// `models.validate_scope`'s grammar.
fn is_scope(text: &str) -> bool {
    let b = text.as_bytes();
    let word = |c: u8| c.is_ascii_lowercase() || c.is_ascii_digit();
    if b.is_empty() || !word(b[0]) || !word(b[b.len() - 1]) {
        return false;
    }
    b.iter().all(|&c| word(c) || c == b'-' || c == b':')
        && !b.windows(2).any(|w| !word(w[0]) && !word(w[1]))
}

fn scope_findings(
    role: &'static str,
    key: &str,
    entries: &[String],
) -> Result<Vec<Finding>, Raised> {
    let mut out = Vec::new();
    for entry in entries {
        if is_scope(entry) {
            continue;
        }
        let candidate = py_lower(py_strip(entry));
        let suggestion = if is_scope(&candidate) {
            format!("did you mean {}?", dumps_unicode(&candidate))
        } else {
            "write the scope the way memory_write takes it".to_owned()
        };
        let (level, effect) = if key == "allowed" {
            (
                ERROR,
                "no write can carry it, so the allowlist admits nothing by it",
            )
        } else {
            (WARNING, "no scope can match it, so the entry is inert")
        };
        out.push(Finding::new(
            level,
            "invalid_scope",
            Some(role),
            Some("scopes"),
            Some(key),
            format!(
                "[scopes] {key} entry {} is not a scope (lowercase letters and digits, joined by - or :); {effect}.",
                dumps_unicode(entry)
            ),
            Some(suggestion),
        ));
    }
    Ok(out)
}

fn acknowledge_findings(role: &'static str, names: &[String]) -> Vec<Finding> {
    let known: Vec<String> = ACKNOWLEDGE_NAMES.iter().map(|n| (*n).to_owned()).collect();
    let mut out = Vec::new();
    for item in names {
        if known.contains(item) {
            continue;
        }
        let bare = item.strip_prefix("acknowledge_").unwrap_or(item);
        let suggestion = if bare != item && known.iter().any(|k| k == bare) {
            format!("write \"{bare}\", without the acknowledge_ prefix")
        } else {
            match close(item, &known, 0.6) {
                Some(guess) => format!("did you mean \"{guess}\"?"),
                None => format!("one of {}", known.join(", ")),
            }
        };
        out.push(Finding::new(
            ERROR,
            "unknown_acknowledge_name",
            Some(role),
            Some("policy"),
            Some("forbid_acknowledge"),
            format!(
                "[policy] forbid_acknowledge names {}, which is not an acknowledge flag of memory_write or memory_update; the config does not load.",
                dumps_unicode(item)
            ),
            Some(suggestion),
        ));
    }
    out
}

/// `config._warn_on_system_dir`: the warning it would log, if any.
fn system_dir_warning(world: &World, source: &str, resolved: &str) -> Option<String> {
    for (raw, prefix) in &world.locations.system_dirs {
        if resolved == prefix || world.fs.under(resolved, prefix) {
            return Some(format!(
                "{source} resolves to {resolved}, which is under a system directory ({raw}). \
                 bettermemory will still try to use it, but this is almost always a \
                 misconfiguration — check your env var or config and point at a \
                 user-writable path."
            ));
        }
    }
    None
}

/// `config_cmd._system_dir_findings`. It catches OSError and RuntimeError
/// only: a `~name` that does not expand finds nothing, and the ValueError
/// a NUL in the directory raises escapes `config check`.
fn system_dir_findings(
    world: &World,
    role: &'static str,
    directory: &str,
) -> Result<Vec<Finding>, Raised> {
    let resolved = match expanded(world, directory) {
        Ok(path) => world.fs.resolve(&path)?,
        Err(raised) if raised.class == "RuntimeError" => return Ok(Vec::new()),
        Err(raised) => return Err(raised),
    };
    if system_dir_warning(world, "[storage] directory", &resolved).is_none() {
        return Ok(Vec::new());
    }
    Ok(vec![Finding::new(
        WARNING,
        "system_directory",
        Some(role),
        Some("storage"),
        Some("directory"),
        format!(
            "[storage] directory = {} resolves to {resolved}, under a system directory; \
             bettermemory will try to use it, but it is almost always a misconfiguration.",
            dumps_unicode(directory)
        ),
        Some("point it at a directory your account owns".into()),
    )])
}

fn port_findings(
    role: &'static str,
    value: &Value,
    port: &toml::Int,
) -> Result<Vec<Finding>, Raised> {
    if port.cmp_i128(0) == Ordering::Less || port.cmp_i128(PORT_MAX) == Ordering::Greater {
        return Ok(vec![Finding::new(
            ERROR,
            "port_out_of_range",
            Some(role),
            Some("daemon"),
            Some("port"),
            format!(
                "[daemon] port = {} is outside 0..{PORT_MAX}; the daemon cannot bind it and does not start.",
                shown(value)?
            ),
            Some(format!("a port from 1024 to {PORT_MAX}, or 0 for an ephemeral one")),
        )]);
    }
    if port.cmp_i128(0) == Ordering::Greater
        && port.cmp_i128(FIRST_UNPRIVILEGED_PORT) == Ordering::Less
    {
        return Ok(vec![Finding::new(
            WARNING,
            "privileged_port",
            Some(role),
            Some("daemon"),
            Some("port"),
            format!(
                "[daemon] port = {} is a privileged port; where an unprivileged process may not bind it (Linux, by default) the daemon binds an ephemeral port instead.",
                shown(value)?
            ),
            Some(format!("a port from 1024 to {PORT_MAX}")),
        )]);
    }
    Ok(Vec::new())
}

/// `config_cmd._parse_one`: the value as the loader reads it alone.
fn parse_one(
    section: &str,
    key: &str,
    value: &Value,
    source: &Source,
    guards: &mut Guards,
) -> Result<Setting, Raised> {
    let mut inner = Table::new();
    inner.insert(key.to_owned(), value.clone());
    let mut data = Table::new();
    data.insert(section.to_owned(), Value::Table(inner));
    let mut ctx = Ctx::new(guards);
    let config = config_from_data(&mut data, source, &mut ctx)?;
    Ok(config.get(section, key).clone())
}

fn value_findings(
    world: &World,
    role: &'static str,
    source: &Source,
    section: &str,
    key: &str,
    value: &Value,
    guards: &mut Guards,
) -> Result<Vec<Finding>, Raised> {
    let label = format!("[{section}] {key}");
    if (section, key) == ("policy", "forbid_acknowledge") {
        if let Value::Array(items) = value {
            if items.iter().all(|v| matches!(v, Value::String(_))) {
                let names: Vec<String> = items
                    .iter()
                    .map(|v| match v {
                        Value::String(t) => t.clone(),
                        _ => unreachable!(),
                    })
                    .collect();
                return Ok(acknowledge_findings(role, &names));
            }
        }
    }
    let parsed = match parse_one(section, key, value, source, guards) {
        Ok(parsed) => parsed,
        Err(raised) if raised.class == "ValueError" => {
            let prefix = format!("malformed config in {}: ", source.text);
            let stripped = raised
                .message
                .strip_prefix(&prefix)
                .unwrap_or(&raised.message);
            return Ok(vec![Finding::new(
                ERROR,
                "invalid_value",
                Some(role),
                Some(section),
                Some(key),
                format!("{stripped}; the config does not load."),
                None,
            )]);
        }
        Err(raised) => return Err(raised),
    };
    if fell_back(section, key, value) {
        let hint = if (section, key) == ("behavior", "prompt_recall") {
            format!("one of {}", PROMPT_RECALL_MODES.join(", "))
        } else {
            "write true or false".to_owned()
        };
        return Ok(vec![Finding::new(
            WARNING,
            "fallback",
            Some(role),
            Some(section),
            Some(key),
            format!(
                "{label} = {} is not a value it takes, so the default, {}, is in force.",
                shown(value)?,
                shown_setting(&parsed)?
            ),
            Some(hint),
        )]);
    }
    let mut findings = Vec::new();
    match ((section, key), &parsed) {
        (("capture", "checkpoint_tokens"), _) => {
            let mut ctx = Ctx::new(guards);
            let written = coerce_int(Some(value), 0, &label, Some(source.text), &mut ctx)?;
            if written.cmp_i128(MIN_CHECKPOINT_TOKENS) == Ordering::Less {
                findings.push(Finding::new(
                    WARNING,
                    "clamped",
                    Some(role),
                    Some(section),
                    Some(key),
                    format!(
                        "{label} = {} is below {MIN_CHECKPOINT_TOKENS}; the loader raises it to {MIN_CHECKPOINT_TOKENS}.",
                        shown(value)?
                    ),
                    Some(format!("write {MIN_CHECKPOINT_TOKENS} or more")),
                ));
            }
        }
        (("daemon", "port"), Setting::Int(port)) => {
            findings.extend(port_findings(role, value, port)?)
        }
        (("scopes", _), Setting::List(entries)) => {
            findings.extend(scope_findings(role, key, entries)?)
        }
        (("storage", "directory"), Setting::Str(directory)) => {
            findings.extend(system_dir_findings(world, role, directory)?)
        }
        _ => {}
    }
    Ok(findings)
}

fn fixed_finding(
    role: &'static str,
    section: &str,
    key: &str,
    value: &Value,
    guards: &mut Guards,
) -> Result<Finding, Raised> {
    let fixed = fixed_of(section, key).expect("a fixed setting").to_raw();
    let mut message = format!(
        "[{section}] {key} is no longer a setting: its value is fixed at {}.",
        shown(&fixed)?
    );
    if !fixed_line_restates(section, key, value, guards) {
        message.push_str(&format!(" This file's {} is not used.", shown(value)?));
    }
    Ok(Finding::new(
        NOTE,
        "fixed",
        Some(role),
        Some(section),
        Some(key),
        message,
        Some("delete the line".into()),
    ))
}

#[allow(clippy::too_many_arguments)]
fn section_findings(
    world: &World,
    file: &ConfigFile,
    section: &str,
    table: &Table,
    pinned: &[((&str, &str), Value)],
    managed: &ConfigFile,
    guards: &mut Guards,
) -> Result<Vec<Finding>, Raised> {
    let mut findings = Vec::new();
    let source = file.source(world.fs);
    let removed = removed_keys(section);
    let keys = section_keys(section);
    for (key, value) in table.iter() {
        if fixed_of(section, key).is_some() {
            findings.push(fixed_finding(file.role, section, key, value, guards)?);
        } else if let Some((_, removed_in, why)) = removed.iter().find(|(k, _, _)| k == key) {
            findings.push(Finding::new(
                WARNING,
                "removed",
                Some(file.role),
                Some(section),
                Some(key),
                format!("[{section}] {key} was removed in bettermemory {removed_in}: {why}. The line is ignored."),
                Some("delete the line".into()),
            ));
        } else if !keys.contains(&key.as_str()) {
            findings.push(unknown_key(file.role, Some(section), key));
        } else if let Some(pin) = lookup(pinned, section, key) {
            let managed_source = managed.source(world.fs);
            if !line_reads_as_pin(section, key, value, pin, &source, &managed_source, guards) {
                findings.push(Finding::new(
                    NOTE,
                    "pinned",
                    Some(file.role),
                    Some(section),
                    Some(key),
                    format!(
                        "[{section}] {key} = {} is ignored: the managed file pins it to {}.",
                        shown(value)?,
                        shown(pin)?
                    ),
                    Some("delete the line".into()),
                ));
            }
        } else {
            findings.extend(value_findings(
                world, file.role, &source, section, key, value, guards,
            )?);
        }
    }
    Ok(findings)
}

fn file_findings(
    world: &World,
    file: &ConfigFile,
    pinned: &[((&str, &str), Value)],
    managed: &ConfigFile,
    guards: &mut Guards,
) -> Result<Vec<Finding>, Raised> {
    if let Some(error) = &file.error {
        return Ok(vec![Finding::new(
            ERROR,
            "unreadable",
            Some(file.role),
            None,
            None,
            error.clone(),
            None,
        )]);
    }
    let Some(data) = &file.data else {
        return Ok(Vec::new());
    };
    let mut findings = Vec::new();
    for (section, value) in data.iter() {
        let is_table = matches!(value, Value::Table(_));
        if file.role == MANAGED && !is_table {
            findings.push(not_a_table(file.role, section, value)?);
        } else if let Some((_, removed_in, why)) =
            REMOVED_SECTIONS.iter().find(|(n, _, _)| n == section)
        {
            findings.push(Finding::new(
                WARNING,
                "removed",
                Some(file.role),
                Some(section),
                None,
                format!("[{section}] was removed in bettermemory {removed_in}: {why}. Every line in it is ignored."),
                Some("delete the section".into()),
            ));
        } else if SECTIONS.contains(&section.as_str()) {
            match value {
                Value::Table(table) => findings.extend(section_findings(
                    world, file, section, table, pinned, managed, guards,
                )?),
                _ => findings.push(not_a_table(file.role, section, value)?),
            }
        } else if is_table {
            findings.push(unknown_section(file.role, section));
        } else {
            findings.push(unknown_key(file.role, None, section));
        }
    }
    Ok(findings)
}

/// `bettermemory config check --json`: the text printed and the exit code.
pub fn check(world: &World, guards: &mut Guards) -> Result<(String, i32), Raised> {
    let (user, managed) = read_files(world)?;
    let pinned = raw_settings(managed.data.as_ref());
    let mut findings = file_findings(world, &user, &pinned, &managed, guards)?;
    findings.extend(file_findings(world, &managed, &[], &managed, guards)?);
    let (loaded, _) = load(world, guards);
    let load_error = match loaded {
        Ok(_) => None,
        Err(raised) if raised.is_value_or_os() => Some(raised.message),
        Err(raised) => Some(format!("{}: {}", raised.class, raised.message)),
    };
    if let Some(error) = &load_error {
        if !findings.iter().any(|f| f.level == ERROR) {
            findings.push(Finding::new(
                ERROR,
                "does_not_load",
                None,
                None,
                None,
                format!("the config does not load: {error}"),
                None,
            ));
        }
    }
    let count = |level: &str| findings.iter().filter(|f| f.level == level).count();
    let (status, code) = if count(ERROR) > 0 {
        (ERROR, 1)
    } else if count(WARNING) > 0 {
        (WARNING, 2)
    } else {
        ("ok", 0)
    };
    let document = dict(vec![
        ("status", s(status)),
        ("exit_code", PyValue::int(i128::from(code))),
        ("loads", PyValue::Bool(load_error.is_none())),
        ("load_error", opt(load_error.as_deref())),
        ("files", PyValue::List(vec![user.to_py(), managed.to_py()])),
        (
            "counts",
            dict(
                LEVELS
                    .iter()
                    .map(|l| (*l, PyValue::int(count(l) as i128)))
                    .collect(),
            ),
        ),
        (
            "findings",
            PyValue::List(findings.iter().map(Finding::to_py).collect()),
        ),
    ]);
    Ok((emit(&document)?, code))
}

/// `bettermemory config show --json`: the text printed and the exit code.
pub fn show(world: &World, guards: &mut Guards) -> Result<(String, i32), Raised> {
    let (user, managed) = read_files(world)?;
    let (loaded, notices) = load(world, guards);
    let config = match loaded {
        Ok(config) => config,
        Err(raised) if raised.is_value_or_os() => {
            return Ok((emit(&dict(vec![("error", s(&raised.message))]))?, 1));
        }
        Err(raised) => return Err(raised),
    };
    let user_raw = raw_settings(user.data.as_ref());
    let managed_raw = raw_settings(managed.data.as_ref());
    let user_source = user.source(world.fs);
    let managed_source = managed.source(world.fs);
    let mut settings = Vec::new();
    for section in SECTIONS {
        let mut table = Vec::new();
        for key in section_keys(section) {
            let value = config.get(section, key);
            let mut entry = vec![("value", value.to_py()?)];
            let written = lookup(&user_raw, section, key);
            if config.is_managed(&format!("{section}.{key}")) {
                entry.push(("source", s(MANAGED)));
                if let Some(written) = written {
                    let reads_as_pin = lookup(&managed_raw, section, key).is_some_and(|pin| {
                        line_reads_as_pin(
                            section,
                            key,
                            written,
                            pin,
                            &user_source,
                            &managed_source,
                            guards,
                        )
                    });
                    if !reads_as_pin {
                        entry.push(("user_value", toml_to_py(written)?));
                    }
                }
            } else if let Some(written) = written {
                if fell_back(section, key, written) {
                    entry.push(("source", s("default")));
                    entry.push(("user_value", toml_to_py(written)?));
                } else {
                    entry.push(("source", s(USER)));
                }
            } else {
                entry.push(("source", s("default")));
            }
            table.push((key, dict(entry)));
        }
        settings.push((section, dict(table)));
    }
    let document = dict(vec![
        ("config", user.to_py()),
        ("managed", managed.to_py()),
        ("settings", dict(settings)),
        (
            "notices",
            PyValue::List(notices.iter().map(|n| s(n)).collect()),
        ),
    ]);
    Ok((emit(&document)?, 0))
}

/// `str(Path(text))` in the world's flavour.
fn pure(world: &World, text: &str) -> String {
    pure_path(text, world.locations.flavour)
}

/// `str(Path(text).expanduser())`, or the RuntimeError it raises.
fn expanded(world: &World, text: &str) -> Result<String, Raised> {
    Ok(pure(world, &world.fs.expanduser(&pure(world, text))?))
}

/// `Config.resolved_directory()` and the warning it logs.
fn resolved_directory(
    world: &World,
    config: &Config,
    notices: &mut Vec<String>,
) -> Result<String, Raised> {
    let fs = world.fs;
    let mut chosen = |source: &str, named: &str| {
        let resolved = fs.resolve(&expanded(world, named)?)?;
        if let Some(warning) = system_dir_warning(world, source, &resolved) {
            notices.push(warning);
        }
        Ok(resolved)
    };
    if let Some(named) = world.env_set(ENV_DIR_OVERRIDE) {
        return chosen(ENV_DIR_OVERRIDE, named);
    }
    if let Setting::Str(directory) = config.get("storage", "directory") {
        if !directory.is_empty() {
            return chosen("[storage] directory", directory);
        }
    }
    let home_default = || fs.resolve(&fs.join(&fs.home().unwrap_or_default(), GLOBAL_DIR_NAME));
    if !config.get("storage", "cwd_store").as_bool() {
        return home_default();
    }
    if let Some(cwd) = fs.cwd() {
        let project = fs.join(&fs.resolve(&cwd)?, PROJECT_DIR_NAME);
        if fs.is_dir(&project) {
            return fs.resolve(&project);
        }
    }
    home_default()
}

/// `sandbox_settings.store_rule`.
fn store_rule(world: &World, config: &Config, directory: &str) -> Result<String, Raised> {
    if world.env_set(ENV_DIR_OVERRIDE).is_some() {
        return Ok(ENV_DIR_OVERRIDE.to_owned());
    }
    if matches!(config.get("storage", "directory"), Setting::Str(d) if !d.is_empty()) {
        return Ok("[storage] directory".to_owned());
    }
    let fs = world.fs;
    if let Some(home) = fs.home() {
        if directory == fs.resolve(&fs.join(&home, GLOBAL_DIR_NAME))? {
            return Ok("home default".to_owned());
        }
    }
    let Some(cwd) = fs.cwd() else {
        return Ok("home default".to_owned());
    };
    if directory == fs.resolve(&fs.join(&fs.resolve(&cwd)?, PROJECT_DIR_NAME))? {
        return Ok(format!("./{PROJECT_DIR_NAME}"));
    }
    Ok("home default".to_owned())
}

fn store_entry(
    world: &World,
    config: Option<&Config>,
    error: Option<&str>,
) -> Result<PyValue, Raised> {
    let fs = world.fs;
    let Some(config) = config else {
        let Some(named) = world.env_set(ENV_DIR_OVERRIDE) else {
            return Ok(dict(vec![
                ("path", PyValue::None),
                ("rule", PyValue::None),
                ("exists", PyValue::Bool(false)),
                (
                    "notes",
                    PyValue::List(vec![s(&format!(
                        "not resolved: the config does not load ({})",
                        error.unwrap_or("None")
                    ))]),
                ),
            ]));
        };
        let directory = fs.resolve(&expanded(world, named)?)?;
        return Ok(dict(vec![
            ("path", s(&directory)),
            ("rule", s(ENV_DIR_OVERRIDE)),
            ("exists", PyValue::Bool(fs.is_dir(&directory))),
            ("notes", PyValue::List(Vec::new())),
        ]));
    };
    let mut notes = Vec::new();
    let directory = resolved_directory(world, config, &mut notes)?;
    let mut rule = store_rule(world, config, &directory)?;
    if rule == "[storage] directory" && config.is_managed("storage.directory") {
        rule.push_str(" (managed)");
    }
    let has_directory =
        matches!(config.get("storage", "directory"), Setting::Str(d) if !d.is_empty());
    if !config.get("storage", "cwd_store").as_bool() && world.env_set(ENV_DIR_OVERRIDE).is_none() {
        if let Some(cwd) = fs.cwd() {
            let here = fs.resolve(&fs.join(&cwd, PROJECT_DIR_NAME))?;
            if fs.is_dir(&here) && here != directory && !has_directory {
                let pinned = if config.is_managed("storage.cwd_store") {
                    " (managed)"
                } else {
                    ""
                };
                notes.push(format!(
                    "{here} is not used: [storage] cwd_store = false{pinned}"
                ));
            }
        }
    }
    Ok(dict(vec![
        ("path", s(&directory)),
        ("rule", s(&rule)),
        ("exists", PyValue::Bool(fs.is_dir(&directory))),
        ("notes", PyValue::List(notes.iter().map(|n| s(n)).collect())),
    ]))
}

/// `bettermemory config path --json`: the text printed and the exit code.
pub fn path(world: &World, guards: &mut Guards) -> Result<(String, i32), Raised> {
    let fs = world.fs;
    let user_path = &world.locations.user_config;
    let managed_file = read_managed_file(world)?;
    let (loaded, _) = load(world, guards);
    let (config, error) = match loaded {
        Ok(config) => (Some(config), None),
        Err(raised) if raised.is_value_or_os() => (None, Some(raised.message)),
        Err(raised) => return Err(raised),
    };
    let sorted = |mut names: Vec<String>| {
        names.sort();
        PyValue::List(names.iter().map(|n| s(n)).collect())
    };
    let managed = dict(vec![
        ("path", s(&managed_file.path)),
        ("exists", PyValue::Bool(managed_file.exists)),
        ("read", PyValue::Bool(true)),
        (
            "pinned",
            config
                .as_ref()
                .map_or(PyValue::None, |c| sorted(c.managed_keys.clone())),
        ),
        (
            "pins_nothing",
            config.as_ref().map_or(PyValue::None, |c| {
                sorted(c.managed_fixed.iter().map(|(k, _)| k.clone()).collect())
            }),
        ),
        ("note", PyValue::None),
    ]);
    // `str(Path(text).expanduser())`: `resolve_keys_dir` and
    // `resolve_state_dir` build a `Path` from the variable's text.
    let located = |env: &str, default: &str, fallback: &str| -> Result<PyValue, Raised> {
        let (dir, source) = match world.env_set(env) {
            Some(named) => (expanded(world, named)?, env.to_owned()),
            None => (expanded(world, default)?, fallback.to_owned()),
        };
        Ok(dict(vec![
            ("path", s(&dir)),
            ("source", s(&source)),
            ("exists", PyValue::Bool(fs.is_dir(&dir))),
        ]))
    };
    // In the reference's order: the keys and state directories, the
    // config file's lstat, then the store.
    let keys = located(
        KEYS_DIR_ENV,
        &world.locations.default_keys_dir,
        "user config directory",
    )?;
    let state = located(
        STATE_DIR_ENV,
        &world.locations.default_state_dir,
        "user state directory",
    )?;
    let config_present = fs.present(user_path)?;
    let store = store_entry(world, config.as_ref(), error.as_deref())?;
    let document = dict(vec![
        (
            "config",
            dict(vec![
                ("path", s(user_path)),
                ("exists", PyValue::Bool(config_present)),
            ]),
        ),
        ("managed", managed),
        ("store", store),
        ("keys", keys),
        ("state", state),
        ("error", opt(error.as_deref())),
    ]);
    Ok((emit(&document)?, if error.is_none() { 0 } else { 1 }))
}

fn on(value: bool) -> &'static str {
    if value {
        "on"
    } else {
        "off"
    }
}

fn audit_finding(
    id: &str,
    status: &str,
    summary: String,
    path: Option<&str>,
    fix: Option<String>,
    details: PyValue,
) -> PyValue {
    dict(vec![
        ("id", s(id)),
        ("status", s(status)),
        ("summary", s(&summary)),
        ("path", opt(path)),
        ("fix", opt(fix.as_deref())),
        ("details", details),
    ])
}

/// The config findings of `security audit` (`posture._settings`) over
/// the config the audit loads (`sandbox_settings.load_config_read_only`):
/// each finding's `to_json()`, or `Err` with the text of the ValueError
/// or OSError that stopped the load.
pub fn audit_settings(
    world: &World,
    guards: &mut Guards,
) -> Result<Result<PyValue, String>, Raised> {
    let (loaded, _) = load(world, guards);
    let config = match loaded {
        Ok(config) => config,
        Err(raised) if raised.is_value_or_os() => return Ok(Err(raised.message)),
        Err(raised) => return Err(raised),
    };
    let flag = |section: &str, key: &str| config.get(section, key).as_bool();
    let port = config.get("daemon", "port").to_py()?;
    let port_text = match &port {
        PyValue::Int(text) => text.clone(),
        _ => unreachable!("the port is an int"),
    };
    let facts = dict(vec![
        (
            "telemetry.enabled",
            PyValue::Bool(flag("telemetry", "enabled")),
        ),
        (
            "capture.stop_checkpoint",
            PyValue::Bool(flag("capture", "stop_checkpoint")),
        ),
        (
            "behavior.write_supersession",
            PyValue::Bool(flag("behavior", "write_supersession")),
        ),
        (
            "membership.strict",
            PyValue::Bool(flag("membership", "strict")),
        ),
        ("daemon.port", port),
        (
            "storage.cwd_store",
            PyValue::Bool(flag("storage", "cwd_store")),
        ),
        (
            "security.strict_provenance",
            PyValue::Bool(flag("security", "strict_provenance")),
        ),
    ]);
    let mut out = vec![audit_finding(
        "config.settings",
        "info",
        format!(
            "telemetry {}, capture checkpoint {}, write supersession {}, membership strict {}, \
             strict provenance {}, daemon port {port_text}",
            on(flag("telemetry", "enabled")),
            on(flag("capture", "stop_checkpoint")),
            on(flag("behavior", "write_supersession")),
            on(flag("membership", "strict")),
            on(flag("security", "strict_provenance")),
        ),
        config.config_path.as_deref(),
        None,
        facts,
    )];
    let forbid_force = flag("policy", "forbid_force");
    let mut forbid_acknowledge = match config.get("policy", "forbid_acknowledge") {
        Setting::List(items) => items.clone(),
        _ => Vec::new(),
    };
    forbid_acknowledge.sort();
    let mut managed = config.managed_keys.clone();
    managed.sort();
    let mut summary = if forbid_force || !forbid_acknowledge.is_empty() {
        let mut refused: Vec<String> = Vec::new();
        if forbid_force {
            refused.push("force".into());
        }
        refused.extend(
            forbid_acknowledge
                .iter()
                .map(|n| format!("acknowledge_{n}")),
        );
        format!("the tools refuse {} from agents", refused.join(", "))
    } else {
        "agents may pass force and every acknowledge flag ([policy] at its defaults)".to_owned()
    };
    if !managed.is_empty() {
        summary.push_str(&format!(
            "; {} key(s) pinned by the managed config",
            managed.len()
        ));
    }
    out.push(audit_finding(
        "config.policy",
        "info",
        summary,
        None,
        None,
        dict(vec![
            ("policy.forbid_force", PyValue::Bool(forbid_force)),
            (
                "policy.forbid_acknowledge",
                PyValue::List(forbid_acknowledge.iter().map(|n| s(n)).collect()),
            ),
            (
                "managed_keys",
                PyValue::List(managed.iter().map(|n| s(n)).collect()),
            ),
        ]),
    ));
    let mut lapsed = config.managed_fixed.clone();
    lapsed.sort_by(|a, b| a.0.cmp(&b.0));
    for (full, value) in lapsed {
        let (section, key) = full.split_once('.').expect("section.key");
        let fixed = fixed_of(section, key).expect("a fixed setting").to_raw();
        let mut summary = format!(
            "the managed config sets [{section}] {key} = {}, which pins nothing: it is no longer \
             a setting, and its fixed value {} is in force",
            toml_spelling(&value)?,
            toml_spelling(&fixed)?
        );
        if !fixed_line_restates(section, key, &value, guards) {
            summary.push_str("; the managed value is not enforced");
        }
        let stricter = fixed_line_was_stricter(section, key, &value, guards);
        let mut fix = "delete the line from the managed config".to_owned();
        if stricter {
            fix.push_str(&format!(
                "; the limit is now the fixed {}, looser than the one it set",
                toml_spelling(&fixed)?
            ));
        }
        out.push(audit_finding(
            "config.managed_fixed",
            if stricter { "warn" } else { "info" },
            summary,
            config.managed_path.as_deref(),
            Some(fix),
            dict(vec![
                ("key", s(&full)),
                ("managed_value", toml_to_py(&value)?),
                ("fixed_value", toml_to_py(&fixed)?),
            ]),
        ));
    }
    Ok(Ok(PyValue::List(out)))
}

/// The real file system, with the home directory and working directory
/// the caller names (`Path.home()` reads `HOME`, or `USERPROFILE` on
/// Windows, from the environment the caller holds).
///
/// NOT FOR USE YET: nothing compares it with `pathlib`, and it only
/// approximates it (`resolve` pops `..` after canonicalising where
/// `realpath` follows the symlink first, `expanduser` takes only `~` and
/// `~/`, never `~user`, `under` is case-sensitive, `is_dir` follows
/// symlinks). A later unit tests it on a real tree (a symlinked home, `..`
/// through a symlink, `~user`, relative paths, a config path that is a
/// directory, an unreadable file, prefixes that differ in case) before any
/// entry point may take it; no function in this crate uses it by default,
/// and only the robustness tests construct one.
pub struct SystemFs {
    pub home: Option<String>,
    pub cwd: Option<String>,
}

fn os_failure(error: &std::io::Error) -> OsFailure {
    use std::io::ErrorKind;
    let class = match error.kind() {
        ErrorKind::PermissionDenied => "PermissionError",
        ErrorKind::IsADirectory => "IsADirectoryError",
        ErrorKind::NotADirectory => "NotADirectoryError",
        ErrorKind::NotFound => "FileNotFoundError",
        _ => "OSError",
    };
    let text = error.to_string();
    let strerror = match text.rfind(" (os error ") {
        Some(i) => text[..i].to_owned(),
        None => text,
    };
    OsFailure {
        class,
        errno: error.raw_os_error().unwrap_or(0),
        strerror,
    }
}

impl SystemFs {
    fn absolute(&self, path: &str) -> std::path::PathBuf {
        let p = std::path::Path::new(path);
        if p.is_absolute() {
            p.to_path_buf()
        } else {
            std::path::Path::new(self.cwd.as_deref().unwrap_or(".")).join(p)
        }
    }
}

impl Fs for SystemFs {
    fn read(&self, path: &str) -> Result<Vec<u8>, Option<OsFailure>> {
        std::fs::read(self.absolute(path)).map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                None
            } else {
                Some(os_failure(&e))
            }
        })
    }

    fn present(&self, path: &str) -> Result<bool, Raised> {
        if path.contains('\0') {
            return Err(nul_in_path("lstat"));
        }
        Ok(match std::fs::symlink_metadata(self.absolute(path)) {
            Ok(_) => true,
            Err(e) => e.kind() != std::io::ErrorKind::NotFound,
        })
    }

    fn is_symlink(&self, path: &str) -> bool {
        std::fs::symlink_metadata(self.absolute(path)).is_ok_and(|m| m.file_type().is_symlink())
    }

    fn is_dir(&self, path: &str) -> bool {
        self.absolute(path).is_dir()
    }

    /// `Path.resolve()` (not strict): the longest existing prefix made
    /// canonical, the rest appended as written, `..` and `.` folded.
    fn resolve(&self, path: &str) -> Result<String, Raised> {
        use std::path::Component;
        if path.contains('\0') {
            return Err(nul_in_path("lstat"));
        }
        let absolute = self.absolute(path);
        let mut out = std::path::PathBuf::new();
        let mut canonical = true;
        for component in absolute.components() {
            match component {
                Component::CurDir => {}
                Component::ParentDir => {
                    out.pop();
                }
                other => out.push(other.as_os_str()),
            }
            if canonical {
                match std::fs::canonicalize(&out) {
                    Ok(c) => out = c,
                    Err(_) => canonical = false,
                }
            }
        }
        Ok(out.to_string_lossy().into_owned())
    }

    fn expanduser(&self, path: &str) -> Result<String, Raised> {
        let rest = if path == "~" {
            Some("")
        } else {
            path.strip_prefix("~/").or_else(|| path.strip_prefix("~\\"))
        };
        match (rest, &self.home) {
            (Some(""), Some(home)) => Ok(home.clone()),
            (Some(rest), Some(home)) => Ok(self.join(home, rest)),
            (Some(_), None) => Err(no_home()),
            // `~name` is never looked up here (not for use: see above).
            (None, _) if path.starts_with('~') => Err(no_home()),
            _ => Ok(path.to_owned()),
        }
    }

    fn join(&self, path: &str, name: &str) -> String {
        std::path::Path::new(path)
            .join(name)
            .to_string_lossy()
            .into_owned()
    }

    fn home(&self) -> Option<String> {
        self.home.as_deref().and_then(|h| self.resolve(h).ok())
    }

    fn cwd(&self) -> Option<String> {
        self.cwd.clone()
    }

    fn under(&self, resolved: &str, prefix: &str) -> bool {
        std::path::Path::new(resolved).starts_with(prefix)
    }
}
