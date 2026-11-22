//! Every case of `project_walk.jsonl` against `project_walk`: each `find`
//! row builds the generator's tree in a fresh folder and walks it with
//! the reference's per-file answers (`pf`) as the parse; each `write` row
//! writes through `std::fs`. POSIX only: links, a mode-000 folder and
//! strerror texts.

#![cfg(unix)]

mod common;

use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use bettermemory_core::fsdata::{Flavour, OsError, PyErr, StatResult, StdFs};
use bettermemory_core::gitfile_rule::hostpath::text::Process;
use bettermemory_core::project_file::{Above, ErrorReason, Found, ProjectFile, ProjectFileError};
use bettermemory_core::project_walk::{find, write, Fault, Walk, WriteEffects};
use bettermemory_core::pyvalue::PyStr;
use common::{assert_count, jsonl, unhex};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn text(p: &[u32]) -> String {
    p.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

struct NoCwd;

impl Process for NoCwd {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        panic!("every path a row gives is absolute")
    }
}

fn scratch(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("pfwalk-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir.canonicalize().unwrap()
}

fn build(base: &Path, tree: &[Value]) {
    let mut modes = Vec::new();
    for entry in tree {
        let path = base.join(entry[0].as_str().unwrap());
        match entry[1].as_str().unwrap() {
            "file" => {
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(&path, unhex(entry[2].as_str().unwrap())).unwrap();
            }
            "dir" => std::fs::create_dir_all(&path).unwrap(),
            "link" => {
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::os::unix::fs::symlink(entry[2].as_str().unwrap(), &path).unwrap();
            }
            _ => modes.push((path, entry[2].as_u64().unwrap() as u32)),
        }
    }
    for (path, bits) in modes {
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(bits)).unwrap();
    }
}

fn clear(base: &Path, tree: &[Value]) {
    for entry in tree {
        if entry[1] == "mode" {
            let path = base.join(entry[0].as_str().unwrap());
            let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755));
        }
    }
    let _ = std::fs::remove_dir_all(base);
}

fn rel(base: &str, path: &[u32]) -> Value {
    let path = text(path);
    if path == base {
        return json!(".");
    }
    if path == "/" {
        return json!("/");
    }
    json!(path
        .strip_prefix(&format!("{base}/"))
        .expect("a walk item lies outside the scratch folder"))
}

fn spell(base: &str, item: &Found) -> Value {
    match item {
        Found::File(f) => {
            json!({"p": rel(base, &f.path), "id": text(&f.id), "n": text(&f.name), "b": f.boundary})
        }
        Found::Error(e) => {
            json!({"p": rel(base, &e.path), "r": e.reason.name(), "d": text(&e.detail)})
        }
    }
}

fn reason(name: &str) -> ErrorReason {
    use ErrorReason::*;
    [
        Unreadable,
        Evicted,
        NotAFile,
        TooLarge,
        Toml,
        NoProjectTable,
        IdMissing,
        IdMalformed,
        NameMalformed,
        WrongType,
    ]
    .into_iter()
    .find(|r| r.name() == name)
    .unwrap()
}

fn unspell(base: &str, v: &Value) -> Found {
    let path = cps(&format!("{base}/{}", v["p"].as_str().unwrap()));
    if let Some(r) = v.get("r") {
        return Found::Error(ProjectFileError {
            path,
            reason: reason(r.as_str().unwrap()),
            detail: cps(v["d"].as_str().unwrap()),
        });
    }
    Found::File(ProjectFile {
        id: cps(v["id"].as_str().unwrap()),
        name: cps(v["n"].as_str().unwrap()),
        boundary: v["b"].as_bool().unwrap(),
        path,
    })
}

fn walk_out(base: &str, w: &Walk) -> Value {
    let files =
        |v: Vec<&ProjectFile>| -> Vec<Value> { v.iter().map(|f| rel(base, &f.path)).collect() };
    let one = |f: Option<&ProjectFile>| f.map_or(Value::Null, |f| rel(base, &f.path));
    let path_of = |i: &Found| match i {
        Found::File(f) => rel(base, &f.path),
        Found::Error(e) => rel(base, &e.path),
    };
    json!({
        "cwd": rel(base, &w.cwd),
        "root": w.repository_root.as_deref().map_or(Value::Null, |r| rel(base, r)),
        "inner": w.inner.iter().map(|i| spell(base, i)).collect::<Vec<_>>(),
        "start": w.above.start.as_deref().map_or(Value::Null, |s| rel(base, s)),
        "above": w.above.items.iter().map(|i| spell(base, i)).collect::<Vec<_>>(),
        "items": w.items().into_iter().map(path_of).collect::<Vec<_>>(),
        "files": files(w.files()),
        "errors": w.errors().iter().map(|e| rel(base, &e.path)).collect::<Vec<_>>(),
        "rootf": one(w.root()),
        "nested": files(w.nested()),
        "folder": one(w.folder()),
        "enclosing": files(w.enclosing()),
        "nearest": one(w.nearest()),
        "boundaries": files(w.boundaries()),
        "place": w.files().iter().map(|f| w.placement(f).unwrap().name()).collect::<Vec<_>>(),
    })
}

fn arg(base: &str, v: &Value) -> Option<PyStr> {
    let r = v.as_str()?;
    Some(if r.starts_with('/') {
        cps(r)
    } else {
        cps(&format!("{base}/{r}"))
    })
}

fn run_find(
    base: &str,
    row: &Value,
    cwd: &Value,
    root: &Value,
    above: Option<Above>,
) -> Result<Walk, Fault> {
    let table = row["pf"].as_object().unwrap();
    let mut look = |path: &[u32], _st: &StatResult| -> Result<Found, Fault> {
        let key = rel(base, path);
        Ok(unspell(base, &table[key.as_str().unwrap()]))
    };
    find(
        &StdFs,
        Flavour::Posix,
        &NoCwd,
        &arg(base, cwd).unwrap(),
        arg(base, root).as_deref(),
        above,
        &mut look,
    )
}

fn check_find(dir: &Path, tree: &[Value], row: &Value) {
    build(dir, tree);
    let base = dir.to_str().unwrap();
    let mut above = None;
    if let Some(cache) = row.get("cache") {
        above = Some(
            run_find(base, row, &cache[0], &cache[1], None)
                .unwrap()
                .above,
        );
    }
    if let Some(add) = row.get("add") {
        std::fs::write(dir.join(add[0].as_str().unwrap()), add[1].as_str().unwrap()).unwrap();
    }
    let got = run_find(base, row, &row["cwd"], &row["root"], above);
    clear(dir, tree);
    match got {
        Ok(w) => assert_eq!(walk_out(base, &w), row["o"], "{row}"),
        Err(Fault::Value(m)) => {
            let m = text(&m).replace(base, "B");
            assert_eq!(json!({"c": "ValueError", "m": m}), row["err"], "{row}");
        }
        Err(other) => panic!("{other:?} for {row}"),
    }
}

struct Disk;

fn io(e: std::io::Error) -> OsError {
    bettermemory_core::fsdata::Numbered::of_io(&e).error
}

impl WriteEffects for Disk {
    type File = std::fs::File;
    fn is_dir(&self, path: &[u32]) -> bool {
        Path::new(&text(path)).is_dir()
    }
    fn create_excl(&self, path: &[u32], mode: u32) -> Result<std::fs::File, OsError> {
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(mode)
            .open(text(path))
            .map_err(io)
    }
    fn write_once(&self, file: &mut std::fs::File, data: &[u8]) -> Result<usize, OsError> {
        std::io::Write::write(file, data).map_err(io)
    }
    fn fsync_file(&self, file: &mut std::fs::File) {
        let _ = file.sync_all();
    }
    fn close(&self, file: std::fs::File) -> Result<(), OsError> {
        drop(file);
        Ok(())
    }
    fn unlink_quietly(&self, path: &[u32]) {
        let _ = std::fs::remove_file(text(path));
    }
    fn atomic_write_bytes(&self, path: &[u32], data: &[u8], mode: u32) -> Result<(), Fault> {
        let target = text(path);
        let tmp = format!("{target}.x.tmp");
        std::fs::write(&tmp, data).map_err(|e| Fault::Os(io(e)))?;
        std::fs::rename(&tmp, &target).map_err(|e| Fault::Os(io(e)))?;
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(mode))
            .map_err(|e| Fault::Os(io(e)))
    }
    fn fsync_dir(&self, _path: &[u32]) {}
}

fn check_write(dir: &Path, row: &Value) {
    let base = dir.to_str().unwrap();
    let name: PyStr = row["name"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c.as_u64().unwrap() as u32)
        .collect();
    let folder = row["dir"].as_str().unwrap();
    let got = write(
        &Disk,
        Flavour::Posix,
        &NoCwd,
        &cps(&format!("{base}/{folder}")),
        &name,
        row["b"].as_bool().unwrap(),
        row["rebind"].as_bool().unwrap(),
        &unhex(row["x"].as_str().unwrap()),
    );
    match got {
        Ok(p) => assert_eq!(
            json!({"id": text(&p.id), "p": rel(base, &p.path)}),
            row["o"],
            "{row}"
        ),
        Err(fault) => {
            let err = match fault {
                Fault::Value(m) => json!({"c": "ValueError", "m": text(&m).replace(base, "B")}),
                Fault::NoDirectory(m) => {
                    json!({"c": "FileNotFoundError", "m": text(&m).replace(base, "B")})
                }
                Fault::Os(e) => json!({"c": "OSError", "e": e.errno.unwrap()}),
                Fault::Encode => json!({"c": "UnicodeEncodeError"}),
                other => panic!("{other:?} for {row}"),
            };
            assert_eq!(err, row["err"], "{row}");
        }
    }
    let normal: PathBuf = Path::new(folder)
        .components()
        .fold(PathBuf::new(), |mut acc, c| {
            match c {
                std::path::Component::ParentDir => {
                    acc.pop();
                }
                std::path::Component::CurDir => {}
                other => acc.push(other),
            }
            acc
        });
    let sha = std::fs::read(dir.join(normal).join("bettermemory.toml"))
        .ok()
        .map(|b| common::hex(&Sha256::digest(&b)));
    assert_eq!(json!(sha), row["sha"], "{row}");
}

#[test]
fn every_case() {
    let rows = jsonl("project_walk.jsonl");
    let cases = rows[0]["cases"].as_u64().unwrap() as usize;
    let tree = rows.iter().find(|r| r["k"] == "tree").unwrap()["entries"]
        .as_array()
        .unwrap()
        .clone();
    let walk_dir = scratch("t");
    let write_dir = scratch("w");
    for sub in ["w", "v", "u"] {
        std::fs::create_dir(write_dir.join(sub)).unwrap();
    }
    std::fs::write(write_dir.join("file"), "x\n").unwrap();
    let mut checked = 0;
    for row in &rows[1..] {
        match row["k"].as_str().unwrap() {
            "tree" => {}
            "find" => check_find(&walk_dir, &tree, row),
            "write" => check_write(&write_dir, row),
            other => panic!("{other}"),
        }
        checked += 1;
    }
    let _ = std::fs::remove_dir_all(&walk_dir);
    let _ = std::fs::remove_dir_all(&write_dir);
    assert_count("project_walk", checked, cases);
}
