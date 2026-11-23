//! POSIX home expansion and the platformdirs user configuration directory.
use crate::config::{self, ConfigError, OsFailure};
use crate::fsdata::PyErr;
use crate::gitfile_rule::follow::fsencode;
use crate::githead::discovery::pure;
use crate::gitsystem::record::surrogateescape;
use crate::pyvalue::repr_str;
use crate::unicode::cps;
use std::env;
use std::ffi::{OsStr, OsString};
use std::io;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};

pub(crate) fn error_text(class: &'static str, message: &str) -> ConfigError {
    ConfigError {
        class,
        message: cps(message),
    }
}
pub(crate) fn bytes(path: &Path) -> &[u8] {
    path.as_os_str().as_bytes()
}
pub(crate) fn path(bytes: Vec<u8>) -> PathBuf {
    OsString::from_vec(bytes).into()
}
pub(crate) fn failure(error: io::Error) -> OsFailure {
    let errno = error
        .raw_os_error()
        .expect("POSIX filesystem error carries errno");
    OsFailure {
        class: PyErr::of_errno(bettermemory_sys::errno_name(errno).unwrap_or("")).name(),
        errno,
        strerror: bettermemory_sys::strerror(errno).unwrap_or_else(|| error.to_string()),
    }
}
pub(crate) fn os_error(error: OsFailure, file: Option<&Path>) -> ConfigError {
    let mut message = cps(&format!("[Errno {}] {}", error.errno, error.strerror));
    if let Some(p) = file {
        message.extend(cps(": "));
        message.extend(repr_str(&surrogateescape(bytes(p))));
    }
    ConfigError {
        class: error.class,
        message,
    }
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
pub(crate) fn expanded(p: &Path) -> Result<PathBuf, ConfigError> {
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
        path(pure(bytes(
            &base.join(OsStr::from_bytes(&raw[split + 1..])),
        )))
    })
}
pub(crate) fn user_config_dir() -> Result<PathBuf, ConfigError> {
    let xdg = env::var_os("XDG_CONFIG_HOME").map(|v| {
        let points = surrogateescape(v.as_bytes());
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
    Ok(path(pure(bytes(&base.join("bettermemory")))))
}
