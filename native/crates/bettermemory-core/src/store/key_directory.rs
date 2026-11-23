//! Select the signing-key directory while keeping its original Python Path spelling.
#[cfg(unix)]
mod posix;
pub mod windows;

#[cfg(unix)]
pub use posix::{default_keys_dir, os_path, resolve_keys_dir, FsEncodeError};
#[cfg(windows)]
pub use windows::{default_keys_dir, os_path, resolve_keys_dir};
