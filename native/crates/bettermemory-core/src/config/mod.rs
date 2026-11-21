//! The settings: what `config.py` and `config_sections.py` make of a
//! config file, and what `bettermemory config path|show|check --json` and
//! the config findings of `security audit` (`posture._settings`) print.
//!
//! - [`config_from_data`] is `config._config_from_data`: one parsed file to
//!   a [`Config`], with the coercions, the located refusals, the removed
//!   keys and sections, the fixed settings dropped unread, and the
//!   warnings the loader logs.
//! - [`load_config`] is `config.load_config(path, create=False)`: the
//!   user's file read (`read_config_file`: `utf-8-sig`, then
//!   [`crate::toml::loads`]) and the organisation's managed file pinned
//!   over it (`_apply_managed`, `managed.read_managed`).
//! - [`create::load_config_creating`] is `load_config(path, create=True)`:
//!   the parent directories made and [`create::DEFAULT_CONFIG`] written
//!   when no file is there, then the same read.
//! - [`report`] holds the three commands' documents and the audit's
//!   findings, each returned as the text the CLI prints and its exit code.
//!
//! Nothing here reads the process environment or the file system
//! directly: the environment is a map ([`Env`]) and every file and path
//! operation goes through a [`Fs`], so a test can hand both in as data and
//! the same inputs give the same text on every platform. [`report::SystemFs`]
//! reads the real file system, and is not for use until a later unit
//! compares it with `pathlib` (see its documentation).
//!
//! The loader's one-shot notices (a removed key, a pinned key, a scalar
//! section) are logged once per process for each file and key
//! (`config._DEPRECATED_KEY_WARNED_PATHS`); [`Guards`] is that set, held by
//! the caller, so two commands run in one process share it as the
//! reference's do.

pub mod create;
pub mod decode;
pub mod difflib;
pub mod report;

use std::collections::{BTreeMap, HashSet};

use crate::json::{self, DumpOptions};
use crate::pyvalue::{self, PyValue};
use crate::toml::{self, Int, Table, Value};
use crate::unicode;

/// The process environment, as data.
pub type Env = BTreeMap<String, String>;

/// `config._DEPRECATED_KEY_WARNED_PATHS`: (the resolved source, the guard
/// key) of each one-shot notice already logged.
pub type Guards = HashSet<(String, String)>;

pub const ENV_DIR_OVERRIDE: &str = "BETTERMEMORY_DIR";
pub const KEYS_DIR_ENV: &str = "BETTERMEMORY_KEYS_DIR";
pub const STATE_DIR_ENV: &str = "BETTERMEMORY_STATE_DIR";
pub const PROJECT_DIR_NAME: &str = ".claude-memory";
pub const GLOBAL_DIR_NAME: &str = ".claude-memory";
/// The environment variables the product reads (`settings.json`'s
/// `environment`): the three locations this module reads, then the
/// caller's identity and Claude Code's, which other units read.
pub const ENVIRONMENT: [&str; 11] = [
    ENV_DIR_OVERRIDE,
    KEYS_DIR_ENV,
    STATE_DIR_ENV,
    "BETTERMEMORY_CLIENT",
    "BETTERMEMORY_CLIENT_VERSION",
    "BETTERMEMORY_MODEL",
    "BETTERMEMORY_WORKSPACE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_CUSTOM_OAUTH_URL",
];

/// `managed.MANAGED_REGISTRY` as it prints.
pub const MANAGED_REGISTRY: &str = "registry HKLM\\SOFTWARE\\Policies\\bettermemory\\ManagedConfig";

/// An exception the reference raises, by class and `str()`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Raised {
    pub class: &'static str,
    pub message: String,
}

impl Raised {
    pub fn value(message: impl Into<String>) -> Raised {
        Raised {
            class: "ValueError",
            message: message.into(),
        }
    }

    /// An `OSError` subclass: `str()` is `[Errno N] strerror: 'path'`.
    pub fn os(err: &OsFailure, path: &str) -> Raised {
        Raised {
            class: err.class,
            message: format!(
                "[Errno {}] {}: {}",
                err.errno,
                err.strerror,
                toml::py_repr_str(path)
            ),
        }
    }

    /// Caught by `except (ValueError, OSError)`.
    pub fn is_value_or_os(&self) -> bool {
        self.class == "ValueError"
            || self.class == "UnicodeDecodeError"
            || self.class == "TOMLDecodeError"
            || OS_CLASSES.contains(&self.class)
    }
}

const OS_CLASSES: [&str; 6] = [
    "OSError",
    "PermissionError",
    "IsADirectoryError",
    "NotADirectoryError",
    "FileNotFoundError",
    "BlockingIOError",
];

/// An `OSError` a read raised: its class, errno and strerror.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OsFailure {
    pub class: &'static str,
    pub errno: i32,
    pub strerror: String,
}

/// The file system and path operations the loader and the reports use,
/// each as the reference's `pathlib` / `os` call answers it.
pub trait Fs {
    /// `path.read_bytes()`; `Err(None)` is FileNotFoundError.
    fn read(&self, path: &str) -> Result<Vec<u8>, Option<OsFailure>>;
    /// `os.lstat(path)` succeeds or fails with anything but
    /// FileNotFoundError (`config_cmd._present`). A relative path is read
    /// against `cwd()`. A path holding a NUL raises, as `os.lstat` does:
    /// [`nul_in_path`]`("lstat")`.
    fn present(&self, path: &str) -> Result<bool, Raised>;
    fn is_symlink(&self, path: &str) -> bool;
    /// `os.path.isdir(path)`, a relative path read against `cwd()`: false
    /// for a path the OS cannot stat, a NUL in it included.
    fn is_dir(&self, path: &str) -> bool;
    /// `Path(path).resolve()`, as text, a relative path resolved against
    /// `cwd()`. A path holding a NUL raises ValueError, as `os.lstat` in
    /// `os.path.realpath` does: [`nul_in_path`]`("lstat")`.
    fn resolve(&self, path: &str) -> Result<String, Raised>;
    /// `Path(path).expanduser()`, as text, of a path already spelled as
    /// `str(Path(...))` spells it: a first segment `~` is the home
    /// directory; a first segment `~name` is that user's home, or, where
    /// there is no such user (`os.path.expanduser` leaves it as written),
    /// `RuntimeError("Could not determine home directory.")`
    /// ([`no_home`]).
    fn expanduser(&self, path: &str) -> Result<String, Raised>;
    /// `path / name`, as text.
    fn join(&self, path: &str, name: &str) -> String;
    /// `Path.home().resolve()`, None where there is no home.
    fn home(&self) -> Option<String>;
    /// `Path.cwd()`, None where it raises.
    fn cwd(&self) -> Option<String>;
    /// `resolved.is_relative_to(prefix)` (or equal).
    fn under(&self, resolved: &str, prefix: &str) -> bool;
}

/// The ValueError an `os` call raises for a path holding a NUL
/// (`os.lstat`: "lstat: embedded null character in path").
pub fn nul_in_path(call: &str) -> Raised {
    Raised::value(format!("{call}: embedded null character in path"))
}

/// The RuntimeError `Path.expanduser()` raises where `~` or `~name` does
/// not expand.
pub fn no_home() -> Raised {
    Raised {
        class: "RuntimeError",
        message: "Could not determine home directory.".to_owned(),
    }
}

/// Which `PurePath` flavour spells a path: `PurePosixPath` or
/// `PureWindowsPath`. The caller's world says which ([`Locations::flavour`]);
/// nothing in the library picks one by the platform it was built for, so a
/// POSIX world reads the same on every runner.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Flavour {
    Posix,
    Windows,
}

/// `str(PurePath(text))` under `flavour`: the spelling `Path` gives text
/// it is built from, before anything touches the file system. Both are
/// `pathlib._local.PurePath._parse_path` and `_format_parsed_parts` of
/// CPython 3.13 ported, over `posixpath.splitroot` and `ntpath.splitroot`.
///
/// POSIX: repeated separators collapse to one, `.` segments and a
/// trailing separator go, `..` stays, and exactly two leading slashes stay
/// two (three or more become one). An empty text is `.`.
///
/// Windows: `/` reads as `\`; the drive is `ntpath.splitroot`'s (a letter
/// and colon, a UNC share `\\server\share`, a device `\\.\name` or
/// `\\?\name`, `\\?\UNC\server\share`), and an incomplete UNC drive is
/// kept as written; a share gains a root; a first segment that reads as a
/// drive (`a:b`) after a relative start is written `.\a:b`. Compared with
/// `PureWindowsPath` over `settings.json`'s `pure_paths`.
pub fn pure_path(text: &str, flavour: Flavour) -> String {
    if text.is_empty() {
        return ".".to_owned();
    }
    let (sep, drv, root, rel) = match flavour {
        Flavour::Posix => {
            let (root, rel) = posix_splitroot(text);
            ('/', String::new(), root.to_owned(), rel.to_owned())
        }
        Flavour::Windows => {
            let path = text.replace('/', "\\");
            let (drv, mut root, rel) = windows_splitroot(&path);
            if root.is_empty() && drv.starts_with('\\') && !drv.ends_with('\\') {
                let parts: Vec<&str> = drv.split('\\').collect();
                // `drv_parts[2] not in '?.'` is a substring test.
                if (parts.len() == 4 && !"?.".contains(parts[2])) || parts.len() == 6 {
                    root = "\\";
                }
            }
            ('\\', drv.to_owned(), root.to_owned(), rel.to_owned())
        }
    };
    let tail: Vec<&str> = rel
        .split(sep)
        .filter(|p| !p.is_empty() && *p != ".")
        .collect();
    let joined = tail.join(&sep.to_string());
    let out = if !drv.is_empty() || !root.is_empty() {
        format!("{drv}{root}{joined}")
    } else if flavour == Flavour::Windows
        && tail.first().is_some_and(|t| t.chars().nth(1) == Some(':'))
    {
        format!(".\\{joined}")
    } else {
        joined
    };
    if out.is_empty() {
        ".".to_owned()
    } else {
        out
    }
}

/// `posixpath.splitroot`, less the empty drive: (root, rest).
fn posix_splitroot(p: &str) -> (&str, &str) {
    if !p.starts_with('/') {
        ("", p)
    } else if !p[1..].starts_with('/') || p[2..].starts_with('/') {
        ("/", &p[1..])
    } else {
        ("//", &p[2..])
    }
}

/// `ntpath.splitroot` of a path whose separators are already `\`:
/// (drive, root, rest).
fn windows_splitroot(p: &str) -> (&str, &str, &str) {
    if let Some(after) = p.strip_prefix('\\') {
        if after.starts_with('\\') {
            let unc = p.len() >= 8 && p.as_bytes()[..8].eq_ignore_ascii_case(b"\\\\?\\UNC\\");
            let start = if unc { 8 } else { 2 };
            let Some(index) = p[start..].find('\\').map(|i| i + start) else {
                return (p, "", "");
            };
            let Some(index2) = p[index + 1..].find('\\').map(|i| i + index + 1) else {
                return (p, "", "");
            };
            return (&p[..index2], &p[index2..=index2], &p[index2 + 1..]);
        }
        return ("", &p[..1], after);
    }
    let mut chars = p.char_indices();
    let first = chars.next();
    match (first, chars.next()) {
        (Some(_), Some((i, ':'))) => {
            let drive_end = i + 1;
            if p[drive_end..].starts_with('\\') {
                (
                    &p[..drive_end],
                    &p[drive_end..=drive_end],
                    &p[drive_end + 1..],
                )
            } else {
                (&p[..drive_end], "", &p[drive_end..])
            }
        }
        _ => ("", "", p),
    }
}

/// Where the managed config is read from.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ManagedSource {
    /// A file (macOS, Linux and every other POSIX system, and wherever a
    /// test points the file seam).
    File(String),
    /// Windows' registry value: the text it holds (None: no value), or the
    /// ValueError reading it raised.
    Registry(Result<Option<String>, String>),
}

impl ManagedSource {
    /// `str(source)`.
    pub fn text(&self) -> String {
        match self {
            ManagedSource::File(path) => path.clone(),
            ManagedSource::Registry(_) => MANAGED_REGISTRY.to_owned(),
        }
    }
}

/// The locations the platform supplies (`platformdirs`, `managed.py`,
/// `config._SYSTEM_DIR_PREFIXES`).
#[derive(Clone, Debug)]
pub struct Locations {
    /// `config.default_config_path()`.
    pub user_config: String,
    pub managed: ManagedSource,
    /// `log.default_keys_dir()`.
    pub default_keys_dir: String,
    /// `platformdirs.user_state_dir("bettermemory")`.
    pub default_state_dir: String,
    /// `_SYSTEM_DIR_PREFIXES`, each as written and as resolved.
    pub system_dirs: Vec<(String, String)>,
    /// How a path built from text is spelled: `Path` is `PosixPath` or
    /// `WindowsPath` on the platform the world stands for.
    pub flavour: Flavour,
}

/// Everything a load reads: the files, the environment, the locations.
pub struct World<'a> {
    pub fs: &'a dyn Fs,
    pub env: &'a Env,
    pub locations: &'a Locations,
}

impl World<'_> {
    /// `os.environ.get(name)`, truthy.
    pub fn env_set(&self, name: &str) -> Option<&str> {
        self.env
            .get(name)
            .map(String::as_str)
            .filter(|v| !v.is_empty())
    }
}

/// What one load logged and noted.
pub struct Ctx<'g> {
    /// Warnings logged on `bettermemory.config`, in order.
    pub log: Vec<String>,
    /// `_TRIAL`'s notes while `_read_alone` reads a line; None otherwise.
    trial: Option<Vec<String>>,
    pub guards: &'g mut Guards,
}

impl<'g> Ctx<'g> {
    pub fn new(guards: &'g mut Guards) -> Ctx<'g> {
        Ctx {
            log: Vec::new(),
            trial: None,
            guards,
        }
    }

    fn warn(&mut self, message: String) {
        match &mut self.trial {
            Some(notes) => notes.push(message),
            None => self.log.push(message),
        }
    }

    fn note(&mut self, message: String) {
        if let Some(notes) = &mut self.trial {
            notes.push(message);
        }
    }

    /// Adds a one-shot guard: true when it was new.
    fn guard(&mut self, source: &str, key: String) -> bool {
        self.guards.insert((source.to_owned(), key))
    }
}

/// A setting's value as the loader holds it.
#[derive(Clone, Debug, PartialEq)]
pub enum Setting {
    None,
    Bool(bool),
    Int(Int),
    Str(String),
    List(Vec<String>),
}

impl Setting {
    pub fn as_bool(&self) -> bool {
        matches!(self, Setting::Bool(true))
    }

    /// The value as TOML would hold it, for the messages that spell it.
    pub fn to_raw(&self) -> Option<Value> {
        Some(match self {
            Setting::None => return None,
            Setting::Bool(b) => Value::Boolean(*b),
            Setting::Int(i) => Value::Integer(i.clone()),
            Setting::Str(s) => Value::String(s.clone()),
            Setting::List(items) => {
                Value::Array(items.iter().map(|s| Value::String(s.clone())).collect())
            }
        })
    }

    /// The value as `json.dumps` meets it.
    pub fn to_py(&self) -> Result<PyValue, Raised> {
        Ok(match self {
            Setting::None => PyValue::None,
            Setting::Bool(b) => PyValue::Bool(*b),
            Setting::Int(i) => PyValue::Int(i.to_decimal().map_err(Raised::value)?),
            Setting::Str(s) => PyValue::str(s),
            Setting::List(items) => PyValue::List(items.iter().map(|s| PyValue::str(s)).collect()),
        })
    }
}

/// The settings' sections and keys, in `Config`'s dataclass order, each
/// with its TOML type name and default (`config_cmd.settings_catalogue`).
pub const CATALOGUE: [(&str, &str, &str); 18] = [
    ("storage", "directory", "string, or absent"),
    ("storage", "cwd_store", "boolean"),
    ("behavior", "write_supersession", "boolean"),
    ("behavior", "prompt_recall", "string"),
    ("behavior", "session_brief", "boolean"),
    ("behavior", "tombstone_retention_days", "integer"),
    ("scopes", "allowed", "array of strings"),
    ("scopes", "typo_exceptions", "array of strings"),
    ("policy", "forbid_force", "boolean"),
    ("policy", "forbid_acknowledge", "array of strings"),
    ("telemetry", "enabled", "boolean"),
    ("daemon", "port", "integer"),
    ("capture", "stop_checkpoint", "boolean"),
    ("capture", "checkpoint_tokens", "integer"),
    ("capture", "daily_cap", "integer"),
    ("membership", "write_token", "boolean"),
    ("membership", "strict", "boolean"),
    ("security", "strict_provenance", "boolean"),
];

/// The sections, in order.
pub const SECTIONS: [&str; 9] = [
    "storage",
    "behavior",
    "scopes",
    "policy",
    "telemetry",
    "daemon",
    "capture",
    "membership",
    "security",
];

/// The keys of one section, in order.
pub fn section_keys(section: &str) -> Vec<&'static str> {
    CATALOGUE
        .iter()
        .filter(|(s, _, _)| *s == section)
        .map(|(_, k, _)| *k)
        .collect()
}

/// `Config()`'s value of a setting.
pub fn default_of(section: &str, key: &str) -> Setting {
    match (section, key) {
        ("storage", "directory") => Setting::None,
        ("storage", "cwd_store") => Setting::Bool(true),
        ("behavior", "write_supersession") => Setting::Bool(true),
        ("behavior", "prompt_recall") => Setting::Str("project".into()),
        ("behavior", "session_brief") => Setting::Bool(true),
        ("behavior", "tombstone_retention_days") => Setting::Int(Int::from_i128(0)),
        ("scopes", _) | ("policy", "forbid_acknowledge") => Setting::List(Vec::new()),
        ("policy", "forbid_force") => Setting::Bool(false),
        ("telemetry", "enabled") => Setting::Bool(true),
        ("daemon", "port") => Setting::Int(Int::from_i128(7397)),
        ("capture", "stop_checkpoint") => Setting::Bool(false),
        ("capture", "checkpoint_tokens") => Setting::Int(Int::from_i128(12_000)),
        ("capture", "daily_cap") => Setting::Int(Int::from_i128(12)),
        ("membership", "write_token") => Setting::Bool(true),
        ("membership", "strict") => Setting::Bool(false),
        ("security", "strict_provenance") => Setting::Bool(false),
        _ => panic!("no setting {section}.{key}"),
    }
}

/// A fixed setting's value (`config.FIXED_SETTINGS`).
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Fixed {
    Int(i64),
    Float(f64),
    Bool(bool),
    Str(&'static str),
}

impl Fixed {
    pub fn to_raw(self) -> Value {
        match self {
            Fixed::Int(i) => Value::Integer(Int::from_i128(i128::from(i))),
            Fixed::Float(f) => Value::Float(f),
            Fixed::Bool(b) => Value::Boolean(b),
            Fixed::Str(s) => Value::String(s.to_owned()),
        }
    }
}

/// `config.FIXED_SETTINGS`, in order: every one is in `[behavior]`.
pub const FIXED_SETTINGS: [(&str, &str, Fixed); 11] = [
    ("behavior", "default_max_results", Fixed::Int(5)),
    (
        "behavior",
        "recency_boost_half_life_days",
        Fixed::Float(30.0),
    ),
    ("behavior", "conversational", Fixed::Bool(true)),
    ("behavior", "search_mode", Fixed::Str("hybrid")),
    ("behavior", "heavily_used_min_applied", Fixed::Int(3)),
    (
        "behavior",
        "cold_endorsement_ratio_threshold",
        Fixed::Float(0.0),
    ),
    ("behavior", "verification_stale_days", Fixed::Int(30)),
    ("behavior", "max_content_bytes", Fixed::Int(1_000_000)),
    ("behavior", "max_takeaway_bytes", Fixed::Int(4_096)),
    ("behavior", "max_scopes_per_write", Fixed::Int(64)),
    ("behavior", "min_content_tokens", Fixed::Int(0)),
];

pub fn fixed_of(section: &str, key: &str) -> Option<Fixed> {
    FIXED_SETTINGS
        .iter()
        .find(|(s, k, _)| *s == section && *k == key)
        .map(|(_, _, v)| *v)
}

/// `config.FIXED_LIMITS`: the direction a stricter value takes.
pub fn fixed_limit(section: &str, key: &str) -> Option<&'static str> {
    match (section, key) {
        ("behavior", "max_content_bytes")
        | ("behavior", "max_takeaway_bytes")
        | ("behavior", "max_scopes_per_write") => Some("below"),
        ("behavior", "min_content_tokens") => Some("above"),
        _ => None,
    }
}

const NINE: &str = "9.0.0";
const HINT_GONE: &str = "the one-shot curation hint on memory_write left with the nine-tool \
     surface; `bettermemory health` carries the counts";
const WASH: &str = "measured a wash on the owner's labelled replay and ruled out of 9.0";

/// `_REMOVED_BEHAVIOR_KEYS`, in order.
pub const REMOVED_BEHAVIOR_KEYS: [(&str, &str, &str); 11] = [
    (
        "corroboration_boost",
        "8.0.0",
        "the ranking nudge it enabled read the `corroborations` rollup, which only bumps \
         when a memory_write is dedup-rejected — a bar prose-sized memories do not reach, \
         so the flag never changed a ranking (deprecated in 7.6.0). The rollup itself \
         stays and still keeps corroborated memories out of dead-weight curation",
    ),
    (
        "require_write_confirmation",
        NINE,
        "the staged-write flow left with the nine-tool surface",
    ),
    (
        "rescue_expansion",
        NINE,
        "the expansion leg is measured by the retrieval bench and no longer a shipped knob",
    ),
    ("endorsement_boost", NINE, WASH),
    ("outcome_demotion", NINE, WASH),
    (
        "standing_tier",
        NINE,
        "the standing section left with the session-start rewrite",
    ),
    (
        "full_tool_surface",
        NINE,
        "the surface is nine tools, always",
    ),
    ("curation_hint_threshold", NINE, HINT_GONE),
    ("curation_hint_enabled", NINE, HINT_GONE),
    (
        "recall_in_project",
        NINE,
        "the prompt recall delivers in the caller's project by default now; \
         `prompt_recall = \"everywhere\"` widens it and `\"off\"` stops it",
    ),
    (
        "endorsement_debt_ratio_threshold",
        NINE,
        "it was renamed `cold_endorsement_ratio_threshold` in 3.2.0 and read under that \
         name until 9.0; that key is a fixed value (0.0) since 9.0.0rc20",
    ),
];

/// `_REMOVED_TELEMETRY_KEYS`.
pub const REMOVED_TELEMETRY_KEYS: [(&str, &str, &str); 2] = [
    (
        "log_queries_verbatim",
        NINE,
        "queries are always redacted in the event log; there is no verbatim shape to restore",
    ),
    ("max_bytes", NINE, "the event log no longer rotates"),
];

/// `_REMOVED_CAPTURE_KEYS`.
pub const REMOVED_CAPTURE_KEYS: [(&str, &str, &str); 2] = [
    (
        "enabled",
        NINE,
        "the 9.0 capture checkpoint asks the chat model at a turn end and is switched on \
         by `stop_checkpoint`, which this line does not set",
    ),
    (
        "idle_minutes",
        NINE,
        "capture no longer catches up sessions that went quiet",
    ),
];

/// `_REMOVED_SECTIONS`.
pub const REMOVED_SECTIONS: [(&str, &str, &str); 2] = [
    (
        "consolidate",
        NINE,
        "unattended consolidation left with the nine-tool surface; `bettermemory health` \
         carries the counts it acted on",
    ),
    (
        "proposals",
        NINE,
        "the write-reflex proposal queue left with the nine-tool surface",
    ),
];

/// `config_cmd._removed_keys(section)`.
pub fn removed_keys(section: &str) -> &'static [(&'static str, &'static str, &'static str)] {
    match section {
        "behavior" => &REMOVED_BEHAVIOR_KEYS,
        "telemetry" => &REMOVED_TELEMETRY_KEYS,
        "capture" => &REMOVED_CAPTURE_KEYS,
        _ => &[],
    }
}

pub const ACKNOWLEDGE_NAMES: [&str; 5] = [
    "credential",
    "transient",
    "scope_mismatch",
    "user_claim",
    "truncation",
];
pub const PROMPT_RECALL_MODES: [&str; 3] = ["project", "everywhere", "off"];
const SEARCH_MODES: [&str; 3] = ["keyword", "bm25", "hybrid"];
pub const MIN_CHECKPOINT_TOKENS: i128 = 500;
/// `_SCALAR_TOLERATED_SECTIONS`.
pub const SCALAR_TOLERATED_SECTIONS: [&str; 3] = ["daemon", "capture", "membership"];

const TRUE_STRINGS: [&str; 4] = ["true", "1", "yes", "on"];
const FALSE_STRINGS: [&str; 5] = ["false", "0", "no", "off", ""];

/// `str.strip()`: Python's whitespace, Unicode's included.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| unicode::is_space(c as u32))
}

/// `str.lower()`.
pub fn py_lower(s: &str) -> String {
    unicode::to_string(&unicode::lower(&unicode::cps(s))).expect("text lowers to text")
}

fn token(s: &str) -> String {
    py_lower(py_strip(s))
}

/// A loaded config: every setting in catalogue order, and where it came
/// from.
#[derive(Clone, Debug)]
pub struct Config {
    values: Vec<Setting>,
    /// `config_path`: the user's file (None for a registry source).
    pub config_path: Option<String>,
    /// `managed_path`, as text.
    pub managed_path: Option<String>,
    /// `managed_keys`, as "section.key".
    pub managed_keys: Vec<String>,
    /// `managed_fixed`: "section.key" -> the managed line's value, in
    /// `FIXED_SETTINGS` order.
    pub managed_fixed: Vec<(String, Value)>,
}

impl Config {
    fn index(section: &str, key: &str) -> usize {
        CATALOGUE
            .iter()
            .position(|(s, k, _)| *s == section && *k == key)
            .unwrap_or_else(|| panic!("no setting {section}.{key}"))
    }

    pub fn get(&self, section: &str, key: &str) -> &Setting {
        &self.values[Config::index(section, key)]
    }

    fn set(&mut self, section: &str, key: &str, value: Setting) {
        self.values[Config::index(section, key)] = value;
    }

    pub fn is_managed(&self, name: &str) -> bool {
        self.managed_keys.iter().any(|k| k == name)
    }
}

/// `toml_spelling(value)`: a value as config.toml writes it.
pub fn toml_spelling(value: &Value) -> Result<String, Raised> {
    Ok(match value {
        Value::Boolean(true) => "true".into(),
        Value::Boolean(false) => "false".into(),
        Value::Float(f) if f.is_nan() => "nan".into(),
        Value::Float(f) if f.is_infinite() => if *f > 0.0 { "inf" } else { "-inf" }.into(),
        Value::Float(f) => crate::float_repr::repr(*f),
        Value::Integer(i) => i.to_decimal().map_err(Raised::value)?,
        Value::String(s) => dumps_unicode(s),
        Value::Array(items) => {
            let parts: Result<Vec<String>, Raised> = items.iter().map(toml_spelling).collect();
            format!("[{}]", parts?.join(", "))
        }
        Value::Table(table) => {
            let mut parts = Vec::new();
            for (k, v) in table.iter() {
                parts.push(format!("{} = {}", toml_key(k), toml_spelling(v)?));
            }
            format!("{{{}}}", parts.join(", "))
        }
        other => toml::py_str_datetime(other).expect("a date or time"),
    })
}

/// `json.dumps(text, ensure_ascii=False)`.
pub fn dumps_unicode(text: &str) -> String {
    let options = DumpOptions {
        ensure_ascii: false,
        ..DumpOptions::DEFAULT
    };
    json::utf8_text(&json::dumps(&PyValue::str(text), options).expect("a str dumps"))
}

/// `json.dumps(text)` (ASCII).
pub fn dumps_ascii(text: &str) -> String {
    json::utf8_text(&json::dumps(&PyValue::str(text), DumpOptions::DEFAULT).expect("a str dumps"))
}

fn toml_key(key: &str) -> String {
    if !key.is_empty()
        && key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    {
        key.to_owned()
    } else {
        dumps_unicode(key)
    }
}

/// `repr(value)`, with `int`'s digit limit raised as ValueError.
fn repr(value: &Value) -> Result<String, Raised> {
    toml::py_repr(value).map_err(Raised::value)
}

fn malformed(
    label: &str,
    value: &Value,
    path: Option<&str>,
    expected: &str,
) -> Result<Raised, Raised> {
    let place = path.map(|p| format!(" in {p}")).unwrap_or_default();
    Ok(Raised::value(format!(
        "malformed config{place}: {label} = {} must be {expected}",
        toml_spelling(value)?
    )))
}

fn refuse<T>(label: &str, value: &Value, path: Option<&str>, expected: &str) -> Result<T, Raised> {
    Err(malformed(label, value, path, expected)?)
}

/// `_coerce_bool`.
pub fn coerce_bool(value: Option<&Value>, default: bool, ctx: &mut Ctx) -> Result<bool, Raised> {
    match value {
        Some(Value::Boolean(b)) => return Ok(*b),
        Some(Value::String(s)) => {
            let t = token(s);
            if TRUE_STRINGS.contains(&t.as_str()) {
                return Ok(true);
            }
            if FALSE_STRINGS.contains(&t.as_str()) {
                return Ok(false);
            }
        }
        _ => {}
    }
    if let Some(v) = value {
        let note = format!("{} is no boolean spelling", repr(v)?);
        ctx.note(note);
    }
    Ok(default)
}

/// `int(float)`: exact, ValueError on NaN, OverflowError on infinity.
fn float_to_int(f: f64) -> Result<Int, Raised> {
    match pyvalue::py_int(&PyValue::Float(f)) {
        Ok(PyValue::Int(ref text)) => Ok(Int::from_decimal_text(text)),
        Err(pyvalue::PyError::OverflowError) => Err(Raised {
            class: "OverflowError",
            message: "cannot convert float infinity to integer".into(),
        }),
        _ => Err(Raised::value("cannot convert float NaN to integer")),
    }
}

/// `_coerce_int`.
pub fn coerce_int(
    value: Option<&Value>,
    default: i128,
    label: &str,
    path: Option<&str>,
    ctx: &mut Ctx,
) -> Result<Int, Raised> {
    let Some(value) = value else {
        return Ok(Int::from_i128(default));
    };
    match value {
        Value::Boolean(_) => refuse(label, value, path, "an integer"),
        Value::Integer(i) => Ok(i.clone()),
        Value::Float(f) => {
            let whole = float_to_int(*f)?;
            if f.fract() != 0.0 {
                let note = format!(
                    "{} truncated to {}",
                    crate::float_repr::repr(*f),
                    whole.to_decimal().map_err(Raised::value)?
                );
                ctx.note(note);
            }
            Ok(whole)
        }
        Value::String(s) => match pyvalue::int_from_str(&unicode::cps(py_strip(s))) {
            Ok(PyValue::Int(ref text)) => Ok(Int::from_decimal_text(text)),
            _ => refuse(label, value, path, "an integer"),
        },
        _ => refuse(label, value, path, "an integer"),
    }
}

fn clamp_checkpoint_tokens(value: Int, path: Option<&str>, ctx: &mut Ctx) -> Result<Int, Raised> {
    if value.cmp_i128(MIN_CHECKPOINT_TOKENS) != std::cmp::Ordering::Less {
        return Ok(value);
    }
    let text = value.to_decimal().map_err(Raised::value)?;
    ctx.note(format!("checkpoint_tokens {text} clamped"));
    let place = path.map(|p| format!(" (in {p})")).unwrap_or_default();
    ctx.warn(format!(
        "[capture] checkpoint_tokens = {text}{place} is below {MIN_CHECKPOINT_TOKENS}; \
         using {MIN_CHECKPOINT_TOKENS}."
    ));
    Ok(Int::from_i128(MIN_CHECKPOINT_TOKENS))
}

/// `_prompt_recall_spelling`.
pub fn prompt_recall_spelling(value: &Value) -> Option<String> {
    match value {
        Value::Boolean(true) => Some("project".into()),
        Value::Boolean(false) => Some("off".into()),
        Value::String(s) => {
            let t = token(s);
            if PROMPT_RECALL_MODES.contains(&t.as_str()) {
                Some(t)
            } else if TRUE_STRINGS.contains(&t.as_str()) {
                Some("project".into())
            } else if FALSE_STRINGS.contains(&t.as_str()) {
                Some("off".into())
            } else {
                None
            }
        }
        _ => None,
    }
}

fn coerce_prompt_recall(
    value: Option<&Value>,
    path: Option<&str>,
    ctx: &mut Ctx,
) -> Result<String, Raised> {
    let Some(value) = value else {
        return Ok("project".into());
    };
    if let Some(mode) = prompt_recall_spelling(value) {
        return Ok(mode);
    }
    ctx.note(format!("prompt_recall {} fell back", repr(value)?));
    let place = path.map(|p| format!(" (in {p})")).unwrap_or_default();
    ctx.warn(format!(
        "[behavior] prompt_recall = {} is not one of {}{place}; falling back to \"project\".",
        toml_spelling(value)?,
        PROMPT_RECALL_MODES.join(", ")
    ));
    Ok("project".into())
}

fn coerce_str_list(
    value: Option<&Value>,
    label: &str,
    path: Option<&str>,
) -> Result<Vec<String>, Raised> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let Value::Array(items) = value else {
        let mut raised = malformed(label, value, path, "a list of strings")?;
        raised
            .message
            .push_str(" — did you forget the [ ] brackets?");
        return Err(raised);
    };
    let mut out = Vec::new();
    for item in items {
        match item {
            Value::String(s) => out.push(s.clone()),
            _ => return refuse(label, item, path, "a string (in a list)"),
        }
    }
    Ok(out)
}

fn coerce_optional_str(
    value: Option<&Value>,
    label: &str,
    path: Option<&str>,
) -> Result<Option<String>, Raised> {
    match value {
        None => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(v) => refuse(label, v, path, "a string path"),
    }
}

fn coerce_switch(
    value: Option<&Value>,
    default: bool,
    label: &str,
    path: Option<&str>,
) -> Result<bool, Raised> {
    match value {
        None => Ok(default),
        Some(Value::Boolean(b)) => Ok(*b),
        Some(v @ Value::String(s)) => {
            let t = token(s);
            if TRUE_STRINGS.contains(&t.as_str()) {
                Ok(true)
            } else if FALSE_STRINGS.contains(&t.as_str()) {
                Ok(false)
            } else {
                refuse(label, v, path, "true or false")
            }
        }
        Some(v) => refuse(label, v, path, "true or false"),
    }
}

fn coerce_acknowledge_names(
    value: Option<&Value>,
    label: &str,
    path: Option<&str>,
) -> Result<Vec<String>, Raised> {
    let names = coerce_str_list(value, label, path)?;
    for name in &names {
        if ACKNOWLEDGE_NAMES.contains(&name.as_str()) {
            continue;
        }
        let mut expected = format!("one of {}", ACKNOWLEDGE_NAMES.join(", "));
        let bare = name.strip_prefix("acknowledge_").unwrap_or(name);
        if bare != name && ACKNOWLEDGE_NAMES.contains(&bare) {
            expected.push_str(&format!(
                " (write \"{bare}\", without the acknowledge_ prefix)"
            ));
        }
        return refuse(label, &Value::String(name.clone()), path, &expected);
    }
    Ok(names)
}

fn warn_removed_key(
    section: &str,
    key: &str,
    removed_in: &str,
    why: &str,
    resolved: &str,
    ctx: &mut Ctx,
) {
    let guard = if section == "behavior" {
        format!("{key}+removed")
    } else {
        format!("{section}.{key}+removed")
    };
    if !ctx.guard(resolved, guard) {
        return;
    }
    ctx.warn(format!(
        "bettermemory: TOML config at {resolved} sets [{section}] `{key}`, which was removed \
         in bettermemory {removed_in} — {why}. The line is ignored; delete it to silence \
         this warning."
    ));
}

/// Where a parsed file came from, as the loader names it.
#[derive(Clone, Debug)]
pub struct Source<'a> {
    /// `str(config_path)`.
    pub text: &'a str,
    /// `_resolved_for_guard(config_path)`, as text.
    pub resolved: String,
    /// False for the registry value, which is no file's path.
    pub is_file: bool,
}

impl<'a> Source<'a> {
    pub fn file(fs: &dyn Fs, path: &'a str) -> Source<'a> {
        Source {
            text: path,
            // `_resolved_for_guard` falls back to the path as given; a path
            // that holds a NUL never opened, so its error never reaches here.
            resolved: fs.resolve(path).unwrap_or_else(|_| path.to_owned()),
            is_file: true,
        }
    }

    pub fn registry() -> Source<'static> {
        Source {
            text: MANAGED_REGISTRY,
            resolved: MANAGED_REGISTRY.to_owned(),
            is_file: false,
        }
    }
}

/// `config._config_from_data`: mutates `data` as the reference does
/// (removed keys and sections and fixed settings dropped).
pub fn config_from_data(
    data: &mut Table,
    source: &Source,
    ctx: &mut Ctx,
) -> Result<Config, Raised> {
    let path = Some(source.text);
    // `_section_table`, in order: a scalar where a section belongs is
    // refused, or (three sections) read as the defaults with a notice.
    let mut tables: Vec<bool> = Vec::new();
    for name in SECTIONS {
        match data.get(name) {
            None => tables.push(false),
            Some(Value::Table(_)) => tables.push(true),
            Some(raw) => {
                if !SCALAR_TOLERATED_SECTIONS.contains(&name) {
                    return refuse(&format!("[{name}]"), raw, path, "a table");
                }
                if ctx.guard(&source.resolved, format!("[{name}] scalar")) {
                    let spelled = toml_spelling(raw)?;
                    ctx.warn(format!(
                        "bettermemory: TOML config at {} writes `{name} = {spelled}` where a \
                         [{name}] section belongs. The line is ignored and the section's \
                         defaults are in force; write its settings under a [{name}] header.",
                        source.resolved
                    ));
                }
                tables.push(false);
            }
        }
    }
    for (section, registry) in [
        ("behavior", &REMOVED_BEHAVIOR_KEYS[..]),
        ("telemetry", &REMOVED_TELEMETRY_KEYS[..]),
        ("capture", &REMOVED_CAPTURE_KEYS[..]),
    ] {
        if !tables[SECTIONS
            .iter()
            .position(|s| *s == section)
            .expect("a section")]
        {
            continue;
        }
        let Some(Value::Table(table)) = data.get_mut(section) else {
            continue;
        };
        for (key, removed_in, why) in registry {
            if table.remove(key).is_some() {
                warn_removed_key(section, key, removed_in, why, &source.resolved, ctx);
            }
        }
    }
    for (name, removed_in, why) in REMOVED_SECTIONS {
        let Some(raw) = data.remove(name) else {
            continue;
        };
        let keys: Vec<String> = match &raw {
            Value::Table(t) => t.keys().cloned().collect(),
            _ => vec![String::new()],
        };
        for key in keys {
            warn_removed_key(name, &key, removed_in, why, &source.resolved, ctx);
        }
    }
    if tables[1] {
        if let Some(Value::Table(behavior)) = data.get_mut("behavior") {
            for (_, key, _) in FIXED_SETTINGS {
                behavior.remove(key);
            }
        }
    }
    let empty = Table::new();
    let section = |name: &str| -> &Table {
        let i = SECTIONS.iter().position(|s| *s == name).expect("a section");
        match (tables[i], data.get(name)) {
            (true, Some(Value::Table(t))) => t,
            _ => &empty,
        }
    };
    let mut values: Vec<Setting> = Vec::new();
    let storage = section("storage");
    values.push(
        match coerce_optional_str(storage.get("directory"), "[storage] directory", path)? {
            Some(s) => Setting::Str(s),
            None => Setting::None,
        },
    );
    values.push(Setting::Bool(coerce_switch(
        storage.get("cwd_store"),
        true,
        "[storage] cwd_store",
        path,
    )?));
    let behavior = section("behavior");
    values.push(Setting::Bool(coerce_bool(
        behavior.get("write_supersession"),
        true,
        ctx,
    )?));
    values.push(Setting::Str(coerce_prompt_recall(
        behavior.get("prompt_recall"),
        path,
        ctx,
    )?));
    values.push(Setting::Bool(coerce_bool(
        behavior.get("session_brief"),
        true,
        ctx,
    )?));
    values.push(Setting::Int(coerce_int(
        behavior.get("tombstone_retention_days"),
        0,
        "[behavior] tombstone_retention_days",
        path,
        ctx,
    )?));
    let scopes = section("scopes");
    values.push(Setting::List(coerce_str_list(
        scopes.get("allowed"),
        "[scopes] allowed",
        path,
    )?));
    values.push(Setting::List(coerce_str_list(
        scopes.get("typo_exceptions"),
        "[scopes] typo_exceptions",
        path,
    )?));
    let policy = section("policy");
    values.push(Setting::Bool(coerce_switch(
        policy.get("forbid_force"),
        false,
        "[policy] forbid_force",
        path,
    )?));
    values.push(Setting::List(coerce_acknowledge_names(
        policy.get("forbid_acknowledge"),
        "[policy] forbid_acknowledge",
        path,
    )?));
    values.push(Setting::Bool(coerce_bool(
        section("telemetry").get("enabled"),
        true,
        ctx,
    )?));
    values.push(Setting::Int(coerce_int(
        section("daemon").get("port"),
        7397,
        "[daemon] port",
        path,
        ctx,
    )?));
    let capture = section("capture");
    values.push(Setting::Bool(coerce_bool(
        capture.get("stop_checkpoint"),
        false,
        ctx,
    )?));
    let tokens = coerce_int(
        capture.get("checkpoint_tokens"),
        12_000,
        "[capture] checkpoint_tokens",
        path,
        ctx,
    )?;
    values.push(Setting::Int(clamp_checkpoint_tokens(tokens, path, ctx)?));
    values.push(Setting::Int(coerce_int(
        capture.get("daily_cap"),
        12,
        "[capture] daily_cap",
        path,
        ctx,
    )?));
    let membership = section("membership");
    values.push(Setting::Bool(coerce_bool(
        membership.get("write_token"),
        true,
        ctx,
    )?));
    values.push(Setting::Bool(coerce_bool(
        membership.get("strict"),
        false,
        ctx,
    )?));
    values.push(Setting::Bool(coerce_bool(
        section("security").get("strict_provenance"),
        false,
        ctx,
    )?));
    Ok(Config {
        values,
        config_path: source.is_file.then(|| source.text.to_owned()),
        managed_path: None,
        managed_keys: Vec::new(),
        managed_fixed: Vec::new(),
    })
}

/// `config._read_alone`: what the loader reads one line as, alone, and
/// whether it took the line as written (no note).
pub fn read_alone(
    section: &str,
    key: &str,
    raw: &Value,
    source: &Source,
    guards: &mut Guards,
) -> Result<(Setting, bool), Raised> {
    let mut inner = Table::new();
    inner.insert(key.to_owned(), raw.clone());
    let mut data = Table::new();
    data.insert(section.to_owned(), Value::Table(inner));
    let mut ctx = Ctx::new(guards);
    ctx.trial = Some(Vec::new());
    let config = config_from_data(&mut data, source, &mut ctx)?;
    let clean = ctx.trial.as_ref().is_some_and(Vec::is_empty);
    Ok((config.get(section, key).clone(), clean))
}

fn reads_as_pin(
    section: &str,
    key: &str,
    raw: &Value,
    source: &Source,
    pinned: &Setting,
    guards: &mut Guards,
) -> bool {
    match read_alone(section, key, raw, source, guards) {
        Ok((read, clean)) => clean && read == *pinned,
        Err(_) => false,
    }
}

/// `config.line_reads_as_pin`.
pub fn line_reads_as_pin(
    section: &str,
    key: &str,
    user_raw: &Value,
    managed_raw: &Value,
    user: &Source,
    managed: &Source,
    guards: &mut Guards,
) -> bool {
    match read_alone(section, key, managed_raw, managed, guards) {
        Ok((pinned, _)) => reads_as_pin(section, key, user_raw, user, &pinned, guards),
        Err(_) => same_type_and_equal(user_raw, managed_raw),
    }
}

/// `type(a) is type(b) and a == b` over TOML values.
fn same_type_and_equal(a: &Value, b: &Value) -> bool {
    std::mem::discriminant(a) == std::mem::discriminant(b) && py_eq(a, b)
}

/// Python's `==` between two parsed TOML values.
pub fn py_eq(a: &Value, b: &Value) -> bool {
    use Value::*;
    match (a, b) {
        (Boolean(x), Boolean(y)) => x == y,
        (Boolean(x), Integer(_) | Float(_)) => py_eq(&Integer(Int::from_i128(i128::from(*x))), b),
        (Integer(_) | Float(_), Boolean(y)) => py_eq(a, &Integer(Int::from_i128(i128::from(*y)))),
        (Integer(x), Integer(y)) => x == y,
        (Float(x), Float(y)) => x == y,
        (Integer(i), Float(f)) | (Float(f), Integer(i)) => {
            f.is_finite() && f.fract() == 0.0 && float_to_int(*f).is_ok_and(|w| w == *i)
        }
        (String(x), String(y)) => x == y,
        (DateTime(x), DateTime(y)) => {
            // Python compares aware datetimes as instants; naive with
            // aware is unequal.
            match (x.offset_us, y.offset_us) {
                (None, None) => x == y,
                (Some(_), Some(_)) => x.to_utc() == y.to_utc(),
                _ => false,
            }
        }
        (Date(x), Date(y)) => x == y,
        (Time(x), Time(y)) => x == y,
        (Array(x), Array(y)) => x.len() == y.len() && x.iter().zip(y).all(|(p, q)| py_eq(p, q)),
        (Table(x), Table(y)) => {
            x.len() == y.len() && x.iter().all(|(k, v)| y.get(k).is_some_and(|w| py_eq(v, w)))
        }
        _ => false,
    }
}

/// `raw == default` for a setting's default.
fn eq_default(raw: &Value, default: &Setting) -> bool {
    default.to_raw().is_some_and(|d| py_eq(raw, &d))
}

/// `_restates_pin_or_default`.
fn restates_pin_or_default(
    section: &str,
    key: &str,
    raw: &Value,
    source: &Source,
    pinned: &Setting,
    guards: &mut Guards,
) -> bool {
    if reads_as_pin(section, key, raw, source, pinned, guards) {
        return true;
    }
    let default = default_of(section, key);
    if (section, key) == ("behavior", "prompt_recall") {
        return prompt_recall_spelling(raw).is_some_and(|read| Setting::Str(read) == default);
    }
    // `_restates_default`: a boolean only restates a boolean.
    if matches!(raw, Value::Boolean(_)) || matches!(default, Setting::Bool(_)) {
        return matches!((raw, &default), (Value::Boolean(r), Setting::Bool(d)) if r == d);
    }
    eq_default(raw, &default)
}

/// `managed.read_managed` and `read_managed_registry`: the managed
/// config's sections, or None when there is none.
pub fn read_managed(world: &World) -> Result<Option<Table>, Raised> {
    let (text, source) = match &world.locations.managed {
        ManagedSource::File(path) => {
            let raw = match world.fs.read(path) {
                Ok(raw) => raw,
                Err(None) => {
                    if world.fs.is_symlink(path) {
                        return Err(Raised::value(format!(
                            "managed config {path} is a symlink to a file that does not exist; \
                             bettermemory will not start without the settings it pins. Restore \
                             its target, or remove the link."
                        )));
                    }
                    return Ok(None);
                }
                Err(Some(failure)) => {
                    return Err(Raised::value(format!(
                        "managed config {path} exists but could not be read ({}); bettermemory \
                         will not start without the settings it pins. Make the file readable \
                         by every account that runs bettermemory, or remove it.",
                        failure.strerror
                    )))
                }
            };
            match decode::decode_utf8_sig(&raw) {
                Ok(text) => (text, path.clone()),
                Err(e) => {
                    return Err(Raised::value(format!(
                        "malformed managed config in {path}: {e}"
                    )))
                }
            }
        }
        ManagedSource::Registry(read) => match read {
            Err(message) => return Err(Raised::value(message.clone())),
            Ok(None) => return Ok(None),
            Ok(Some(text)) => (
                text.strip_prefix('\u{feff}').unwrap_or(text).to_owned(),
                MANAGED_REGISTRY.to_owned(),
            ),
        },
    };
    let data = parse_managed_text(&text, &source)?;
    for (name, value) in data.iter() {
        if !matches!(value, Value::Table(_)) {
            return Err(Raised::value(format!(
                "malformed managed config in {source}: `{name}` = {} must be a [{name}] section table",
                repr(value)?
            )));
        }
    }
    Ok(Some(data))
}

/// `managed.parse_managed_text`.
pub fn parse_managed_text(text: &str, source: &str) -> Result<Table, Raised> {
    toml::loads(text).map_err(|e| match e {
        toml::Error::Decode(m) => {
            Raised::value(format!("malformed managed config in {source}: {m}"))
        }
        other => raised_of_toml(other),
    })
}

fn raised_of_toml(e: toml::Error) -> Raised {
    match e {
        toml::Error::Decode(m) => Raised {
            class: "TOMLDecodeError",
            message: m,
        },
        toml::Error::IntDigits(m) => Raised::value(m),
        toml::Error::Recursion => Raised {
            class: "RecursionError",
            message: "maximum recursion depth exceeded".into(),
        },
    }
}

/// Why `read_config_file` returned no table.
#[derive(Clone, Debug)]
pub enum FileError {
    /// FileNotFoundError.
    NotFound,
    /// Another OSError from the read.
    Os(OsFailure),
    /// What the decode or the parse raised.
    Raised(Raised),
}

/// `config.read_config_file`.
pub fn read_config_file(fs: &dyn Fs, path: &str) -> Result<Table, FileError> {
    let raw = match fs.read(path) {
        Ok(raw) => raw,
        Err(None) => return Err(FileError::NotFound),
        Err(Some(failure)) => return Err(FileError::Os(failure)),
    };
    let text = decode::decode_utf8_sig(&raw).map_err(|e| {
        FileError::Raised(Raised::value(format!("malformed config in {path}: {e}")))
    })?;
    toml::loads(&text).map_err(|e| {
        FileError::Raised(match e {
            toml::Error::Decode(m) => Raised::value(format!("malformed config in {path}: {m}")),
            other => raised_of_toml(other),
        })
    })
}

/// `config.load_config(path, create=False)`.
pub fn load_config(world: &World, path: &str, ctx: &mut Ctx) -> Result<Config, Raised> {
    let data = match read_config_file(world.fs, path) {
        Ok(data) => data,
        Err(FileError::NotFound) => Table::new(),
        Err(FileError::Os(failure)) => return Err(Raised::os(&failure, path)),
        Err(FileError::Raised(raised)) => return Err(raised),
    };
    overlay_managed(world, data, path, ctx)
}

/// `config._overlay_managed`: the managed file's pins over one parsed user
/// file.
fn overlay_managed(
    world: &World,
    mut data: Table,
    path: &str,
    ctx: &mut Ctx,
) -> Result<Config, Raised> {
    let user = Source::file(world.fs, path);
    let Some(mut managed_data) = read_managed(world)? else {
        return config_from_data(&mut data, &user, ctx);
    };
    let managed = match &world.locations.managed {
        ManagedSource::File(p) => Source::file(world.fs, p),
        ManagedSource::Registry(_) => Source::registry(),
    };
    apply_managed(&mut data, &user, &mut managed_data, &managed, ctx)
}

/// `config._apply_managed`.
fn apply_managed(
    data: &mut Table,
    user: &Source,
    managed_data: &mut Table,
    managed: &Source,
    ctx: &mut Ctx,
) -> Result<Config, Raised> {
    let mut lapsed = Vec::new();
    for (section, key, _) in FIXED_SETTINGS {
        if let Some(Value::Table(t)) = managed_data.get(section) {
            if let Some(v) = t.get(key) {
                lapsed.push((format!("{section}.{key}"), v.clone()));
            }
        }
    }
    let pinned = config_from_data(managed_data, managed, ctx)?;
    let mut keys: Vec<(String, String)> = Vec::new();
    for (section, raw) in managed_data.iter() {
        let Value::Table(table) = raw else {
            continue;
        };
        if !SECTIONS.contains(&section.as_str()) {
            continue;
        }
        let known = section_keys(section);
        for key in table.keys() {
            if known.contains(&key.as_str()) {
                keys.push((section.clone(), key.clone()));
            }
        }
    }
    for (section, key) in &keys {
        let Some(Value::Table(user_section)) = data.get_mut(section) else {
            continue;
        };
        let Some(user_value) = user_section.remove(key) else {
            continue;
        };
        let pin = pinned.get(section, key).clone();
        if !restates_pin_or_default(section, key, &user_value, user, &pin, ctx.guards) {
            let guard = format!("{section}.{key}+managed:{}", managed.text);
            if ctx.guard(&user.resolved, guard) {
                ctx.warn(format!(
                    "bettermemory: TOML config at {} sets [{section}] `{key}`, which the \
                     managed config at {} pins. The managed value is in force and this line \
                     is ignored; delete it to silence this warning.",
                    user.resolved, managed.text
                ));
            }
        }
    }
    let mut config = config_from_data(data, user, ctx)?;
    for (section, key) in &keys {
        config.set(section, key, pinned.get(section, key).clone());
    }
    config.managed_path = Some(managed.text.to_owned());
    config.managed_keys = keys.iter().map(|(s, k)| format!("{s}.{k}")).collect();
    config.managed_fixed = lapsed;
    Ok(config)
}

/// What 9.0.0rc19 read a fixed setting's line as (`fixed_line_reading`);
/// None for a line it refused.
pub fn fixed_line_reading(
    section: &str,
    key: &str,
    value: &Value,
    guards: &mut Guards,
) -> Option<Value> {
    let fixed = fixed_of(section, key).expect("a fixed setting");
    if key == "search_mode" {
        // `str(value).strip().lower()`: only a string can spell a mode.
        if let Value::String(s) = value {
            let t = token(s);
            if SEARCH_MODES.contains(&t.as_str()) {
                return Some(Value::String(t));
            }
        }
        return Some(Value::String("hybrid".into()));
    }
    if let Fixed::Bool(b) = fixed {
        let mut ctx = Ctx::new(guards);
        return coerce_bool(Some(value), b, &mut ctx)
            .ok()
            .map(Value::Boolean);
    }
    if matches!(value, Value::Boolean(_)) {
        return None;
    }
    match fixed {
        Fixed::Float(_) => match value {
            Value::String(s) => py_float_from_str(py_strip(s)).map(Value::Float),
            Value::Float(f) => Some(Value::Float(*f)),
            Value::Integer(i) => i.to_f64().map(Value::Float),
            _ => None,
        },
        Fixed::Int(i) => {
            let mut ctx = Ctx::new(guards);
            coerce_int(Some(value), i128::from(i), key, None, &mut ctx)
                .ok()
                .map(Value::Integer)
        }
        _ => None,
    }
}

/// `fixed_line_restates`.
pub fn fixed_line_restates(section: &str, key: &str, value: &Value, guards: &mut Guards) -> bool {
    let fixed = fixed_of(section, key).expect("a fixed setting").to_raw();
    match fixed_line_reading(section, key, value, guards) {
        Some(read) => same_type_and_equal(&read, &fixed),
        None => false,
    }
}

/// `fixed_line_was_stricter`.
pub fn fixed_line_was_stricter(
    section: &str,
    key: &str,
    value: &Value,
    guards: &mut Guards,
) -> bool {
    let Some(direction) = fixed_limit(section, key) else {
        return false;
    };
    let Some(Value::Integer(ref read)) = fixed_line_reading(section, key, value, guards) else {
        return false;
    };
    let Some(Fixed::Int(fixed)) = fixed_of(section, key) else {
        return false;
    };
    let fixed = i128::from(fixed);
    if direction == "below" {
        read.cmp_i128(0).is_gt() && read.cmp_i128(fixed).is_lt()
    } else {
        read.cmp_i128(fixed).is_gt()
    }
}

/// `float(text)` for a `str` (already stripped of Python whitespace):
/// Unicode decimal digits read as ASCII ones, underscores between digits,
/// `inf`, `infinity` and `nan` in any case.
pub fn py_float_from_str(text: &str) -> Option<f64> {
    let mut ascii = String::new();
    for c in text.chars() {
        let cp = c as u32;
        if c.is_ascii() {
            ascii.push(c);
        } else if unicode::is_space(cp) {
            ascii.push(' ');
        } else if unicode::is_decimal(cp) {
            let mut start = cp;
            while start > 0 && unicode::is_decimal(start - 1) {
                start -= 1;
            }
            ascii.push(char::from_digit((cp - start) % 10, 10)?);
        } else {
            return None;
        }
    }
    let body = ascii.trim_matches(|c: char| c.is_ascii_whitespace());
    let (sign, rest) = match body.as_bytes().first() {
        Some(b'-') => (-1.0, &body[1..]),
        Some(b'+') => (1.0, &body[1..]),
        _ => (1.0, body),
    };
    let lower = rest.to_ascii_lowercase();
    if lower == "inf" || lower == "infinity" {
        return Some(sign * f64::INFINITY);
    }
    if lower == "nan" {
        return Some(if sign < 0.0 { -f64::NAN } else { f64::NAN });
    }
    // Digits, an optional fraction, an optional exponent; underscores
    // only between two digits.
    let bytes = rest.as_bytes();
    let mut cleaned = String::new();
    let mut digits = 0;
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'_' {
            let before = i > 0 && bytes[i - 1].is_ascii_digit();
            let after = bytes.get(i + 1).is_some_and(u8::is_ascii_digit);
            if !(before && after) {
                return None;
            }
            continue;
        }
        if b.is_ascii_digit() {
            digits += 1;
        } else if !matches!(b, b'.' | b'e' | b'E' | b'+' | b'-') {
            return None;
        }
        cleaned.push(char::from(b));
    }
    if digits == 0 || cleaned.starts_with(['+', '-']) {
        return None;
    }
    let (mantissa, exponent) = match cleaned.find(['e', 'E']) {
        Some(i) => (&cleaned[..i], Some(&cleaned[i + 1..])),
        None => (cleaned.as_str(), None),
    };
    if mantissa.is_empty()
        || mantissa.matches('.').count() > 1
        || !mantissa.bytes().any(|b| b.is_ascii_digit())
        || mantissa.contains(['+', '-'])
    {
        return None;
    }
    if let Some(e) = exponent {
        let digits = e.strip_prefix(['+', '-']).unwrap_or(e);
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
    }
    cleaned.parse::<f64>().ok().map(|v| sign * v)
}

#[cfg(test)]
mod pure_path_tests {
    use super::{pure_path, Flavour};

    /// Each spelling against `str(PurePosixPath(...))` and
    /// `str(PureWindowsPath(...))` as CPython 3.13 prints them.
    #[test]
    fn spellings_as_pathlib_prints_them() {
        for (text, want) in [
            ("", "."),
            (".", "."),
            ("///a", "/a"),
            ("//a/./b//", "//a/b"),
            ("a/", "a"),
            ("../x/.", "../x"),
            ("/", "/"),
            ("<scratch>/a/./b/", "<scratch>/a/b"),
            ("<scratch>/a/../b", "<scratch>/a/../b"),
        ] {
            assert_eq!(pure_path(text, Flavour::Posix), want, "{text:?}");
        }
        for (text, want) in [
            ("c:/a//b/./", "c:\\a\\b"),
            ("//server/share/x", "\\\\server\\share\\x"),
            ("C:", "C:"),
            ("C:a/b", "C:a\\b"),
            ("\\\\a", "\\\\a"),
            ("x\\.\\y\\", "x\\y"),
            ("/a", "\\a"),
        ] {
            assert_eq!(pure_path(text, Flavour::Windows), want, "{text:?}");
        }
    }
}
