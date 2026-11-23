//! Select the POSIX store file for a read-only command. Nothing in this
//! operation creates a configuration, opens a store or acquires a lock.
use super::{path_text, refuse};
use crate::cli::{Exit, UsageError};
use crate::config::{self, Config, ConfigError, Labels, Notices, OsFailure};
use crate::fsdata::{realpath::realpath, Flavour, PyErr, StdFs};
use crate::gitfile_rule::follow::fsencode;
use crate::githead::discovery::pure;
use crate::pyvalue::{repr_str, PyStr};
use crate::store::create::STORE_FILENAME;
use crate::toml::Table;
use crate::unicode::cps;
use std::collections::HashSet;
use std::env;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::io;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};

/// One-shot configuration notices belong to the process that runs commands.
pub type PathGuards = HashSet<(PyStr, PyStr)>;

pub const SYSTEM_WARNING: &str = "%s resolves to %s, which is under a system directory (%s). bettermemory will still try to use it, but this is almost always a misconfiguration — check your env var or config and point at a user-writable path.";

/// A config notice, or the arguments to the system-directory log record.
#[derive(Debug, PartialEq, Eq)]
pub enum PathWarning<'a> {
    Config(&'a [u32]),
    System {
        source: &'static str,
        directory: &'a Path,
        prefix: &'a Path,
    },
}

/// The actual exception from a synchronous logger. The caller supplies its
/// Python catch family separately from its name. A caught error needs the
/// text it would contribute to the CLI refusal.
#[derive(Debug, PartialEq, Eq)]
pub enum WarningError<E> {
    Value(E, PyStr),
    Os(E, PyStr),
    Other(E),
}

#[derive(Debug, PartialEq, Eq)]
pub enum PathError<E> {
    Exit(Exit),
    Raised(ConfigError),
    Logger(E),
}

struct HostNotices<'a, E, F> {
    guards: &'a mut PathGuards,
    warn: &'a mut F,
    raised: Option<WarningError<E>>,
}

impl<E, F: FnMut(PathWarning<'_>) -> Result<(), WarningError<E>>> Notices
    for HostNotices<'_, E, F>
{
    fn warn(&mut self, message: PyStr) -> Result<(), ConfigError> {
        (self.warn)(PathWarning::Config(&message)).map_err(|error| {
            self.raised = Some(error);
            // The typed error, including its original identity, is returned
            // by the public boundary immediately after evaluation exits.
            error_text("Exception", "config warning handler raised")
        })
    }
    fn guard(&mut self, source: &[u32], key: PyStr) -> bool {
        self.guards.insert((source.to_vec(), key))
    }
    fn note(&mut self, _: String) {}
}

fn error_text(class: &'static str, message: &str) -> ConfigError {
    ConfigError {
        class,
        message: cps(message),
    }
}
fn bytes(path: &Path) -> &[u8] {
    path.as_os_str().as_bytes()
}
fn path(bytes: Vec<u8>) -> PathBuf {
    OsString::from_vec(bytes).into()
}
fn failure(error: io::Error) -> OsFailure {
    let errno = error
        .raw_os_error()
        .expect("POSIX filesystem error carries errno");
    OsFailure {
        class: PyErr::of_errno(bettermemory_sys::errno_name(errno).unwrap_or("")).name(),
        errno,
        strerror: bettermemory_sys::strerror(errno).unwrap_or_else(|| error.to_string()),
    }
}
fn os_error(error: OsFailure, file: Option<&Path>) -> ConfigError {
    let mut message = cps(&format!("[Errno {}] {}", error.errno, error.strerror));
    if let Some(p) = file {
        message.extend(cps(": "));
        message.extend(repr_str(&path_text(p)));
    }
    ConfigError {
        class: error.class,
        message,
    }
}

fn resolved(p: &Path) -> Result<PathBuf, ConfigError> {
    let cwd = if p.is_absolute() {
        PathBuf::new()
    } else {
        env::current_dir().map_err(|e| os_error(failure(e), None))?
    };
    realpath(&StdFs, Flavour::Posix, bytes(p), bytes(&cwd))
        .map(path)
        .map_err(|_| config::nul_in_path("lstat").into())
}

/// The empty HOME spells '/' in pathlib, while platformdirs treats it as
/// absent and consults the current account. Named users always use passwd.
fn home(name: Option<&[u8]>, config_home: bool) -> Result<Option<PathBuf>, ConfigError> {
    let env_home = env::var_os("HOME").filter(|v| !config_home || !v.is_empty());
    let h = match (name, env_home) {
        (None, Some(v)) => Some(PathBuf::from(v)),
        _ => {
            if name.is_some_and(|n| n.contains(&0)) {
                return Err(error_text("ValueError", "embedded null byte"));
            }
            bettermemory_sys::account_home(name).map_err(|e| os_error(failure(e), None))?
        }
    };
    Ok(h.filter(|p| !bytes(p).starts_with(b"~")).map(|p| {
        let b = bytes(&p);
        let end = b.iter().rposition(|v| *v != b'/').map_or(0, |i| i + 1);
        if end == 0 {
            PathBuf::from("/")
        } else {
            path(pure(&b[..end]))
        }
    }))
}
fn expanded(p: &Path) -> Result<PathBuf, ConfigError> {
    let raw = pure(bytes(p));
    if !raw.starts_with(b"~") {
        return Ok(path(raw));
    }
    let split = raw.iter().position(|b| *b == b'/').unwrap_or(raw.len());
    let named = &raw[1..split];
    let base = home((!named.is_empty()).then_some(named), false)?.ok_or_else(config::no_home)?;
    Ok(if split == raw.len() {
        base
    } else {
        base.join(OsStr::from_bytes(&raw[split + 1..]))
    })
}
fn default_user() -> Result<PathBuf, ConfigError> {
    let xdg = env::var_os("XDG_CONFIG_HOME").map(|v| {
        let points = path_text(Path::new(&v));
        path(fsencode(crate::claims::strip(&points)).expect("os.fsdecode round trip"))
    });
    let base = match xdg.filter(|p| p.is_absolute()) {
        Some(p) => p,
        None => {
            let suffix = if cfg!(target_os = "macos") {
                "Library/Application Support"
            } else {
                ".config"
            };
            let home = home(None, true)?.ok_or_else(|| {
                let reason=format!("could not determine the home directory for '~/{suffix}', set HOME or an absolute XDG variable");
                error_text("RuntimeError", &reason)
            })?;
            home.join(suffix)
        }
    };
    Ok(path(pure(bytes(&base.join("bettermemory/config.toml")))))
}
fn labels(p: &Path) -> Labels {
    Labels {
        text: path_text(p),
        resolved: resolved(p).map_or_else(|_| path_text(p), |r| path_text(&r)),
        is_file: true,
    }
}
fn read_file(p: &Path) -> Result<Vec<u8>, Option<OsFailure>> {
    fs::read(p).map_err(|e| {
        if e.kind() == io::ErrorKind::NotFound {
            None
        } else {
            Some(failure(e))
        }
    })
}
fn load(user: &Path, managed: &Path, ctx: &mut dyn Notices) -> Result<Config<PyStr>, ConfigError> {
    if bytes(user).contains(&0) {
        return Err(error_text("ValueError", "embedded null byte"));
    }
    let mut data = match read_file(user) {
        Ok(bytes) => config::user_bytes(&bytes, &path_text(user))?,
        Err(None) => Table::new(),
        Err(Some(error)) => return Err(os_error(error, Some(user))),
    };
    if bytes(managed).contains(&0) {
        return Err(error_text("ValueError", "embedded null byte"));
    }
    let pins = config::managed_bytes(
        read_file(managed),
        || managed.is_symlink(),
        &path_text(managed),
    )?;
    config::overlay(&mut data, &labels(user), pins, || labels(managed), ctx)
}

fn named<E>(
    source: &'static str,
    p: PathBuf,
    warn: &mut impl FnMut(PathWarning<'_>) -> Result<(), WarningError<E>>,
) -> Result<PathBuf, PathError<E>> {
    let directory = expanded(&p)
        .and_then(|p| resolved(&p))
        .map_err(PathError::Raised)?;
    for raw in [
        "/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys",
    ] {
        let prefix = Path::new(raw);
        let physical = resolved(prefix).unwrap_or_else(|_| prefix.to_owned());
        if directory.starts_with(physical) {
            match warn(PathWarning::System {
                source,
                directory: &directory,
                prefix,
            }) {
                Ok(()) => break,
                Err(WarningError::Value(..)) => continue,
                Err(WarningError::Other(e) | WarningError::Os(e, _)) => {
                    return Err(PathError::Logger(e))
                }
            }
        }
    }
    Ok(directory)
}

fn caught(parser: &impl UsageError, message: PyStr, code: i32) -> Exit {
    refuse(
        parser,
        &[cps("the config does not load: "), message].concat(),
        code,
    )
}

fn load_error<E>(parser: &impl UsageError, error: ConfigError, code: i32) -> PathError<E> {
    if config::value_or_os(error.class) {
        PathError::Exit(caught(parser, error.message, code))
    } else {
        PathError::Raised(error)
    }
}

/// Use these real files for a caller with explicit config locations. Cwd,
/// environment, account database and symlinks still come from the process.
pub fn read_only_store_path_at<E>(
    parser: &impl UsageError,
    code: i32,
    user: &Path,
    managed: &Path,
    guards: &mut PathGuards,
    warn: &mut impl FnMut(PathWarning<'_>) -> Result<(), WarningError<E>>,
) -> Result<PathBuf, PathError<E>> {
    let mut ctx = HostNotices {
        guards,
        warn,
        raised: None,
    };
    // Both source arguments are pathlib.Path objects at the reference entry.
    let user = path(pure(bytes(user)));
    let managed = path(pure(bytes(managed)));
    let config = load(&user, &managed, &mut ctx).map_err(|error| match ctx.raised.take() {
        Some(WarningError::Value(_, m) | WarningError::Os(_, m)) => {
            PathError::Exit(caught(parser, m, code))
        }
        Some(WarningError::Other(e)) => PathError::Logger(e),
        None => load_error(parser, error, code),
    })?;
    let environment = env::var_os(config::ENV_DIR_OVERRIDE)
        .filter(|v| !v.is_empty())
        .map(PathBuf::from);
    let directory = config::directory::select(
        &config,
        environment,
        |p| PathBuf::from(p),
        |s, p| named(s, p, warn),
        || {
            let Ok(cwd) = env::current_dir() else {
                return Ok(None);
            };
            let Ok(cwd) = resolved(&cwd) else {
                return Ok(None);
            };
            let project = cwd.join(config::PROJECT_DIR_NAME);
            if project.is_dir() {
                resolved(&project).map(Some).map_err(PathError::Raised)
            } else {
                Ok(None)
            }
        },
        || {
            let h = expanded(Path::new("~")).map_err(PathError::Raised)?;
            resolved(&h.join(config::GLOBAL_DIR_NAME)).map_err(PathError::Raised)
        },
    )?;
    Ok(directory.join(STORE_FILENAME))
}

/// POSIX `cli._common.read_only_store_path`. A missing exit code means 2.
pub fn read_only_store_path<E>(
    parser: &impl UsageError,
    exit_code: Option<i32>,
    guards: &mut PathGuards,
    warn: &mut impl FnMut(PathWarning<'_>) -> Result<(), WarningError<E>>,
) -> Result<PathBuf, PathError<E>> {
    let code = exit_code.unwrap_or(2);
    let user = default_user().map_err(|e| load_error(parser, e, code))?;
    let managed = if cfg!(target_os = "macos") {
        "/Library/Application Support/bettermemory/managed.toml"
    } else {
        "/etc/bettermemory/managed.toml"
    };
    read_only_store_path_at(parser, code, &user, Path::new(managed), guards, warn)
}
