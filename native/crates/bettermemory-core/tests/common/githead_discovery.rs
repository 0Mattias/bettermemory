//! The cases of `githead_discovery.jsonl` as a file system in memory
//! (`fsdata::MemFs`) or on disk, and the native answers of
//! `githead::discovery` in the fixture's spelling.

use std::collections::BTreeSet;

use bettermemory_core::fsdata::{Flavour, Fs, MemFs, Node, OsError};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::githead::discovery::{
    across_filesystems, any_entry_above, discovery_moved, find_gitdir, gitdir_at, locate, located,
    probed, stamp, Env, Found, Host, Owners,
};
use serde_json::{json, Value};

/// The folder the cases are laid under in memory: one folder below the
/// root, as a scratch directory always is, so a walk past the case meets a
/// folder before the root.
pub const MEM_ROOT: &str = "/tmp/<scratch>";
/// The uid of every path in memory, and of the user `same` names.
pub const ME: u32 = 1000;

/// A fixture byte string: `=` and itself, or hex.
pub fn unspelled(text: &str) -> Vec<u8> {
    match text.strip_prefix('=') {
        Some(rest) => rest.as_bytes().to_vec(),
        None => super::unhex(text),
    }
}

fn b(data: &[u8]) -> String {
    if data
        .iter()
        .all(|&c| (0x20..0x7f).contains(&c) && c != b'\\')
    {
        return format!("={}", std::str::from_utf8(data).expect("ASCII"));
    }
    super::hex(data)
}

pub fn replaced(data: &[u8], from: &[u8], to: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len());
    let mut rest = data;
    while !rest.is_empty() {
        if rest.starts_with(from) {
            out.extend_from_slice(to);
            rest = &rest[from.len()..];
        } else {
            out.push(rest[0]);
            rest = &rest[1..];
        }
    }
    out
}

pub fn entries<'a>(case: &'a Value, key: &str) -> Vec<(&'a str, &'a Value)> {
    match case.get(key) {
        Some(Value::Object(map)) => map.iter().map(|(k, v)| (k.as_str(), v)).collect(),
        Some(Value::Array(list)) => list
            .iter()
            .map(|v| (v.as_str().unwrap(), &Value::Null))
            .collect(),
        _ => Vec::new(),
    }
}

/// A file's bytes: a string (with `<root>`), `{"p", "x", "n"}` or
/// `{"hex"}`.
pub fn text(value: &Value, root: &str) -> Vec<u8> {
    match value {
        Value::Object(map) if map.contains_key("hex") => super::unhex(map["hex"].as_str().unwrap()),
        Value::Object(map) => {
            let mut out = map
                .get("p")
                .and_then(Value::as_str)
                .unwrap_or("")
                .replace("<root>", root)
                .into_bytes();
            let n = usize::try_from(map["n"].as_u64().unwrap()).unwrap();
            out.extend(map["x"].as_str().unwrap().repeat(n).into_bytes());
            out
        }
        other => other.as_str().unwrap().replace("<root>", root).into_bytes(),
    }
}

/// The case's environment.
pub fn env_of(case: &Value, root: &str) -> Env {
    let mut env = Env::default();
    for (name, value) in entries(case, "env") {
        let value = Some(value.as_str().unwrap().replace("<root>", root).into_bytes());
        match name {
            "GIT_DIR" => env.git_dir = value,
            "GIT_WORK_TREE" => env.git_work_tree = value,
            "GIT_COMMON_DIR" => env.git_common_dir = value,
            "GIT_DISCOVERY_ACROSS_FILESYSTEM" => env.across_filesystem = value,
            "GIT_TEST_ASSUME_DIFFERENT_OWNER" => env.assume_different_owner = value,
            "GIT_CEILING_DIRECTORIES" => env.ceilings = value,
            "SUDO_UID" => env.sudo_uid = value,
            // Read by no function here.
            _ => {}
        }
    }
    env
}

/// The effective user the case names, `me` being the generating user's.
pub fn euid_of(case: &Value, me: u32) -> u32 {
    match case.get("euid").and_then(Value::as_str) {
        None | Some("same") => me,
        Some("other") => me + 1,
        Some("root") => 0,
        Some(other) => panic!("euid {other}"),
    }
}

/// Owners in memory: `owner`, or `owner + 1` for the case's `"o"` paths,
/// after an `lstat` that must answer.
pub struct Owned<'a> {
    pub fs: &'a MemFs,
    pub foreign: BTreeSet<Vec<u8>>,
    pub owner: u32,
}

impl Owners for Owned<'_> {
    fn owner_uid(&self, path: &[u8], follow: bool) -> Result<u32, OsError> {
        if follow {
            self.fs.stat(path)?;
        } else {
            self.fs.lstat_result(path)?;
        }
        Ok(if self.foreign.contains(path) {
            self.owner + 1
        } else {
            self.owner
        })
    }
}

/// The case in memory.
pub fn world(case: &Value) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    let at = |rel: &str| format!("{MEM_ROOT}/{rel}").into_bytes();
    fs.insert(MEM_ROOT.as_bytes(), Node::Dir);
    for (rel, value) in entries(case, "f") {
        fs.insert(&at(rel), Node::File(text(value, MEM_ROOT)));
    }
    for (rel, _) in entries(case, "d") {
        fs.insert(&at(rel), Node::Dir);
    }
    for (rel, to) in entries(case, "l") {
        let target = to.as_str().unwrap().replace("<root>", MEM_ROOT);
        fs.insert(&at(rel), Node::Link(target.into_bytes()));
    }
    for (rel, _) in entries(case, "p") {
        fs.insert(&at(rel), Node::Fifo);
    }
    for (rel, mode) in entries(case, "m") {
        fs.set_mode(
            &at(rel),
            u32::from_str_radix(mode.as_str().unwrap(), 8).unwrap(),
        );
    }
    for (rel, dev) in entries(case, "mnt") {
        fs.mount(&at(rel), dev.as_u64().unwrap());
    }
    fs
}

pub fn foreign_of(case: &Value, root: &str) -> BTreeSet<Vec<u8>> {
    entries(case, "o")
        .into_iter()
        .map(|(rel, _)| format!("{root}/{rel}").into_bytes())
        .collect()
}

pub fn failure(f: Failure) -> Value {
    match f {
        Failure::NotPorted => json!({"native": "NotPorted"}),
        Failure::Value => json!({"e": "ValueError"}),
        Failure::Unsettled => json!({"e": "_Unsettled"}),
        Failure::Raised(name) => json!({"e": name}),
        Failure::Os(e) => match e.errno {
            Some(n) => json!({"e": e.class.name(), "n": n}),
            None => json!({"e": e.class.name()}),
        },
    }
}

fn spelled(data: &[u8], root: &str) -> Value {
    json!(b(&replaced(data, root.as_bytes(), b"<scratch>")))
}

pub fn gd(found: Option<Found>, root: &str) -> Value {
    match found {
        None => Value::Null,
        Some(f) => json!({
            "g": spelled(&f.gd.gitdir, root),
            "c": spelled(&f.gd.commondir, root),
            "w": spelled(&f.worktree_root, root),
        }),
    }
}

fn under(path: &[u8], root: &str) -> bool {
    let root = root.as_bytes();
    path == root || (path.starts_with(root) && path.get(root.len()) == Some(&b'/'))
}

/// What the case's queries are asked with.
pub struct Given<'a, F: Fs, O: Owners> {
    pub fs: &'a F,
    pub owners: &'a O,
    pub euid: u32,
    pub env: Env,
    pub root: &'a str,
}

/// Each query's answer, in the fixture's spelling.
pub fn answers<F: Fs, O: Owners>(given: &Given<'_, F, O>, case: &Value) -> Vec<Value> {
    let root = given.root;
    let ctx = Ctx {
        fs: given.fs,
        flavour: Flavour::Posix,
        cwd: root.as_bytes(),
    };
    let git = GitFacts {
        release: Some((2, 54)),
        reads_compat: Some(true),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &git,
    };
    let host = Host {
        euid: given.euid,
        owners: given.owners,
    };
    let env = &given.env;
    let mut stamps = Vec::new();
    let mut out = Vec::new();
    for q in case["q"].as_array().unwrap() {
        let query = unspelled(q["p"].as_str().unwrap());
        let colon = query.iter().position(|&c| c == b':').unwrap();
        let (op, raw) = (&query[..colon], &query[colon + 1..]);
        let args: Vec<Vec<u8>> = raw
            .split(|&c| c == b'|')
            .map(|a| replaced(a, b"<root>", root.as_bytes()))
            .collect();
        let flags: &[u8] = args.get(1).map_or(b"", Vec::as_slice);
        let v = match op {
            b"f" => gd(find_gitdir(&rule, &host, env, &args[0]), root),
            b"a" => gd(gitdir_at(&rule, &host, env, &args[0]), root),
            b"L" => gd(
                locate(
                    &rule,
                    env,
                    &args[0],
                    !flags.contains(&b'N'),
                    flags.contains(&b'G'),
                ),
                root,
            ),
            b"X" => located(&rule, env, &args[0]).map_or_else(failure, |f| gd(f, root)),
            b"E" => any_entry_above(&ctx, &args[0]).map_or_else(failure, |v| json!(v)),
            b"P" => {
                let device = match flags {
                    b"d" => Some(given.fs.stat(&args[0]).unwrap().dev.unwrap()),
                    b"0" => Some(0),
                    _ => None,
                };
                let mut seen = Vec::new();
                let mut past = false;
                let mut shown = serde_json::Map::new();
                match probed(&ctx, env, &args[0], device) {
                    Err(e) => {
                        shown.insert("e".into(), failure(e));
                    }
                    Ok(walk) => {
                        for d in walk {
                            match d {
                                Ok(d) if under(&d, root) => seen.push(spelled(&d, root)),
                                Ok(_) => past = true,
                                Err(e) => {
                                    shown.insert("e".into(), failure(e));
                                    break;
                                }
                            }
                        }
                    }
                }
                shown.insert("y".into(), json!(seen));
                shown.insert("past".into(), json!(past));
                Value::Object(shown)
            }
            b"S" => match stamp(&ctx, &args[0], !flags.contains(&b'n')) {
                Err(e) => failure(e),
                Ok(None) => Value::Null,
                Ok(Some(got)) => {
                    stamps.push(got);
                    let eq = stamps.iter().position(|s| *s == got).unwrap();
                    let mode = got[4];
                    let mut shown = json!({"t": mode >> 12, "eq": eq});
                    if mode >> 12 == 0o10 {
                        shown["s"] = json!(got[2]);
                    }
                    shown
                }
            },
            b"M" => json!(discovery_moved(env)),
            b"A" => json!(across_filesystems(env)),
            other => panic!("op {other:?}"),
        };
        out.push(json!({"p": q["p"], "v": v}));
    }
    out
}

/// What a test asks a tree in memory with.
pub struct Asked<'a> {
    pub rule: &'a Rule<'a, MemFs, Hung, NoRecords>,
    pub host: &'a Host<'a, Owned<'a>>,
    pub env: &'a Env,
    pub ctx: &'a Ctx<'a, MemFs>,
}

/// Runs `f` over `fs` with the working directory `/`, the effective user
/// `euid` and `foreign` owned by `ME + 1`.
pub fn asking<T>(
    fs: &MemFs,
    env: &Env,
    euid: u32,
    foreign: &[&str],
    f: impl FnOnce(&Asked<'_>) -> T,
) -> T {
    asking_owned(fs, env, euid, ME, foreign, f)
}

/// [`asking`] with every path but `foreign` owned by `owner`.
pub fn asking_owned<T>(
    fs: &MemFs,
    env: &Env,
    euid: u32,
    owner: u32,
    foreign: &[&str],
    f: impl FnOnce(&Asked<'_>) -> T,
) -> T {
    let ctx = Ctx {
        fs,
        flavour: fs.flavour,
        cwd: b"/",
    };
    let git = GitFacts {
        release: Some((2, 54)),
        reads_compat: Some(true),
    };
    let hung = Hung::default();
    let rule = Rule {
        ctx: &ctx,
        answers: &hung,
        records: &NoRecords,
        env: None,
        git: &git,
    };
    let owners = Owned {
        fs,
        foreign: foreign.iter().map(|p| p.as_bytes().to_vec()).collect(),
        owner,
    };
    let host = Host {
        euid,
        owners: &owners,
    };
    f(&Asked {
        rule: &rule,
        host: &host,
        env,
        ctx: &ctx,
    })
}

/// A git directory's files at `at` (`HEAD`, `objects`, `refs`).
pub fn repo(fs: &mut MemFs, at: &str) {
    fs.insert(
        format!("{at}/HEAD").as_bytes(),
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(format!("{at}/objects").as_bytes(), Node::Dir);
    fs.insert(format!("{at}/refs").as_bytes(), Node::Dir);
}
