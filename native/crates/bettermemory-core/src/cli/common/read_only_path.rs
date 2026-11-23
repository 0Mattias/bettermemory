//! Select the POSIX store file for a read-only command. Nothing in this
//! operation creates a configuration, opens a store or acquires a lock.
use super::{path_text, refuse};
use crate::cli::{Exit, UsageError};
use crate::config::posix::{bytes, error_text, expanded, failure, os_error, path, user_config_dir};
use crate::config::{self, Config, ConfigError, Labels, Notices, OsFailure};
use crate::fsdata::{realpath::realpath, Flavour, StdFs};
use crate::githead::discovery::pure;
use crate::pyvalue::PyStr;
use crate::store::create::STORE_FILENAME;
use crate::toml::Table;
use crate::unicode::cps;
use std::collections::HashSet;
use std::env;
use std::fs;
use std::io;
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
    let user = user_config_dir()
        .map_err(|e| load_error(parser, e, code))?
        .join("config.toml");
    let managed = if cfg!(target_os = "macos") {
        "/Library/Application Support/bettermemory/managed.toml"
    } else {
        "/etc/bettermemory/managed.toml"
    };
    read_only_store_path_at(parser, code, &user, Path::new(managed), guards, warn)
}
