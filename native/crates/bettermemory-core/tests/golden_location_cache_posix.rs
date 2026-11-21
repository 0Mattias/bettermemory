//! Every case of `location_cache_posix.jsonl`, POSIX only: `signature`
//! over trees built in scratch, discovery answered as the reference's
//! walk answered it, each stamp taken here of the path the reference's
//! stamp was of.

#![cfg(unix)]

mod common;

use bettermemory_core::capture::Kind;
use bettermemory_core::fsdata::{Flavour, Fs, StdFs};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::location_cache::{
    signature, stamp, CwdPart, DiscoverFailed, RepoSignature, Signature, Stamp,
};
use bettermemory_core::location_git::Git;
use common::parity::Ledger;
use serde_json::Value;
use std::os::unix::fs::PermissionsExt;

const FILE: &str = "location_cache_posix.jsonl";

struct Scratch(std::path::PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        for entry in walk(&self.0) {
            let _ = std::fs::set_permissions(&entry, std::fs::Permissions::from_mode(0o700));
        }
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn walk(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
    let mut out = Vec::new();
    if let Ok(list) = std::fs::read_dir(dir) {
        for e in list.flatten() {
            let p = e.path();
            out.push(p.clone());
            if p.is_dir() {
                out.extend(walk(&p));
            }
        }
    }
    out
}

fn bytes_of(text: &str) -> Vec<u8> {
    text.chars()
        .map(|c| u8::try_from(u32::from(c)).unwrap())
        .collect()
}

fn build(base: &str, items: &Value) {
    std::fs::create_dir_all(base).unwrap();
    for item in items.as_array().unwrap() {
        let path = format!("{base}/{}", item[1].as_str().unwrap());
        match item[0].as_str().unwrap() {
            "d" => std::fs::create_dir_all(&path).unwrap(),
            "f" => {
                let p = std::path::Path::new(&path);
                std::fs::create_dir_all(p.parent().unwrap()).unwrap();
                let text = item[2].as_str().unwrap().replace('@', base);
                std::fs::write(p, bytes_of(&text)).unwrap();
            }
            _ => {
                let mode = item[2].as_u64().unwrap() as u32;
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
            }
        }
    }
}

fn at(base: &str, rel: &Value) -> Option<Vec<u8>> {
    rel.as_str().map(|r| r.replacen('@', base, 1).into_bytes())
}

fn git_of(base: &str, g: &Value) -> Git {
    let kind = Kind::ALL
        .into_iter()
        .find(|k| k.as_str() == g["k"].as_str().unwrap())
        .unwrap();
    Git {
        dotgit: at(base, &g["dg"]),
        git_dir: at(base, &g["gd"]).unwrap(),
        common_dir: at(base, &g["cd"]).unwrap(),
        root: at(base, &g["root"]),
        kind,
    }
}

fn slot(ctx: &Ctx<'_, StdFs>, base: &str, v: &Value) -> Option<Stamp> {
    if v.is_null() {
        return None;
    }
    if v.as_i64() == Some(-1) {
        return Some([-1; 5]);
    }
    let path = at(base, &v["s"]).unwrap();
    Some(stamp(ctx, &path).unwrap().expect("a stamped path"))
}

fn four(ctx: &Ctx<'_, StdFs>, base: &str, v: &Value) -> Option<[i128; 4]> {
    let path = at(base, &v["s"])?;
    let st = ctx.fs.stat(&path).unwrap();
    Some([
        st.mtime_ns,
        st.ctime_ns.unwrap(),
        i128::from(st.size),
        i128::from(st.ino.unwrap()),
    ])
}

fn cwd_part(ctx: &Ctx<'_, StdFs>, base: &str, rel: &Value) -> CwdPart {
    let path = at(base, rel).unwrap();
    let st = ctx.fs.stat(&path).unwrap();
    CwdPart {
        path,
        id: Some((st.dev.unwrap(), st.ino.unwrap())),
    }
}

fn expected(ctx: &Ctx<'_, StdFs>, base: &str, a: &Value) -> Option<Signature> {
    if a.is_null() {
        return None;
    }
    if !a["o"].is_null() {
        return Some(Signature::Outside {
            cwd: cwd_part(ctx, base, &a["o"]),
            project: slot(ctx, base, &a["p"]),
        });
    }
    let r = &a["r"];
    let s = |v: &Value| slot(ctx, base, v);
    Some(Signature::Repo(Box::new(RepoSignature {
        cwd: cwd_part(ctx, base, &r["cwd"]),
        dotgit: at(base, &r["dg"]),
        dotgit_stamp: s(&r["dgs"]),
        git_dir: at(base, &r["gd"]).unwrap(),
        common_dir: at(base, &r["cd"]).unwrap(),
        head: hex(r["h"].as_str().unwrap()),
        head_stat: four(ctx, base, &r["hs"]).unwrap(),
        ref_part: s(&r["ref"]),
        packed: s(&r["pk"]),
        config: four(ctx, base, &r["cf"]),
        shallow: s(&r["sh"]),
        token: s(&r["tk"]),
        inner: r["in"].as_array().unwrap().iter().map(s).collect(),
        refs: r["rf"].as_array().map(|p| (s(&p[0]), s(&p[1]))),
    })))
}

fn hex(t: &str) -> Vec<u8> {
    (0..t.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&t[i..i + 2], 16).unwrap())
        .collect()
}

#[test]
fn location_cache_posix_every_case() {
    let lines = common::jsonl(FILE);
    assert_eq!(lines[0]["cases"].as_u64(), Some(lines.len() as u64 - 1));
    let cases = &lines[1..];
    assert!(!cases.is_empty());
    let ledger = Ledger::new("golden_location_cache_posix", FILE);
    let tmp = std::env::temp_dir().join(format!("bm-location-cache-{}", std::process::id()));
    std::fs::create_dir_all(&tmp).unwrap();
    let scratch = Scratch(std::fs::canonicalize(&tmp).unwrap());
    let top = scratch.0.to_str().unwrap().to_owned();
    let mut failures = Vec::new();
    for (n, case) in cases.iter().enumerate() {
        let base = format!("{top}/{n}");
        build(&base, &case["t"]);
        let ctx = Ctx {
            fs: &StdFs,
            flavour: Flavour::Posix,
            cwd: b"/",
        };
        let calls = case["d"].as_array().unwrap();
        let mut asked = Vec::new();
        let mut discover = |_: &[u8], read_head: bool| -> Result<Option<Git>, DiscoverFailed> {
            let call = &calls[asked.len()];
            asked.push(read_head);
            assert_eq!(call[0].as_bool(), Some(read_head), "{}", case["note"]);
            match &call[1] {
                Value::Null => Ok(None),
                Value::String(_) => Err(DiscoverFailed::Os),
                g => Ok(Some(git_of(&base, g))),
            }
        };
        let cwd = format!("{base}/{}", case["cwd"].as_str().unwrap());
        let moved = case.get("mv").is_some();
        let got = signature(&ctx, cwd.as_bytes(), false, moved, &mut discover).unwrap();
        let count = asked.len();
        let want = expected(&ctx, &base, &case["a"]);
        if got != want || count != calls.len() {
            failures.push(format!(
                "#{n} {}: native {got:?} reference {want:?} ({count} discoveries)",
                case["note"]
            ));
        }
        for entry in walk(std::path::Path::new(&base)) {
            let _ = std::fs::set_permissions(&entry, std::fs::Permissions::from_mode(0o700));
        }
    }
    ledger.settle(cases.len(), &failures);
}
