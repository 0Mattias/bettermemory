//! Physical POSIX tests run inside one private child process per case, so
//! changing cwd or HOME never changes a parallel test or its caller.
#![allow(dead_code)]
use bettermemory_core::argparse::ArgumentParser;
use bettermemory_core::cli::common::read_only_path::{
    read_only_store_path_at, PathError, PathGuards,
};
use std::collections::BTreeMap;
use std::env;
use std::ffi::OsString;
use std::fs;
use std::os::unix::fs::FileTypeExt;
use std::path::{Path, PathBuf};
use std::process::Command;

pub fn parser() -> ArgumentParser {
    ArgumentParser::new("bettermemory log", None, true)
}

pub fn isolated(name: &str, body: impl FnOnce(&Path)) {
    let marker = "BM_TEST_READONLY_PATH_CHILD";
    if env::var_os(marker).as_deref() == Some(name.as_ref()) {
        body(&env::current_dir().unwrap());
        return;
    }
    let tmp = crate::common::stores::scratch(name);
    let home = tmp.0.join("home");
    fs::create_dir(&home).unwrap();
    let mut command = Command::new(env::current_exe().unwrap());
    command
        .args(["--exact", name, "--nocapture", "--test-threads=1"])
        .current_dir(&tmp.0)
        .env(marker, name)
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", tmp.0.join("config"))
        .env_remove("BETTERMEMORY_DIR")
        .env("BETTERMEMORY_KEYS_DIR", tmp.0.join("unused-keys"))
        .env("BETTERMEMORY_STATE_DIR", tmp.0.join("unused-state"));
    if let Some(p) = env::var_os("BM_GOLDEN_DIR") {
        command.env("BM_GOLDEN_DIR", fs::canonicalize(p).unwrap());
    }
    let out = command.output().unwrap();
    assert!(
        out.status.success(),
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
}
pub fn selected(root: &Path) -> Result<PathBuf, PathError<()>> {
    read_only_store_path_at(
        &parser(),
        1,
        &root.join("user.toml"),
        &root.join("managed.toml"),
        &mut PathGuards::new(),
        &mut |_| Ok(()),
    )
}

#[derive(Debug, PartialEq, Eq)]
pub enum Entry {
    Dir,
    File(Vec<u8>),
    Link(PathBuf),
    Fifo,
}
pub fn snapshot(root: &Path) -> BTreeMap<OsString, Entry> {
    fn walk(base: &Path, dir: &Path, out: &mut BTreeMap<OsString, Entry>) {
        for item in fs::read_dir(dir).unwrap() {
            let p = item.unwrap().path();
            let ft = fs::symlink_metadata(&p).unwrap().file_type();
            let value = if ft.is_symlink() {
                Entry::Link(fs::read_link(&p).unwrap())
            } else if ft.is_dir() {
                walk(base, &p, out);
                Entry::Dir
            } else if ft.is_fifo() {
                Entry::Fifo
            } else {
                Entry::File(fs::read(&p).unwrap())
            };
            out.insert(p.strip_prefix(base).unwrap().as_os_str().to_owned(), value);
        }
    }
    let mut out = BTreeMap::new();
    walk(root, root, &mut out);
    out
}
