//! The scripted world of `origin_reach_walk.jsonl`, as
//! `gen_origin_reach._WalkWorld` runs it: git replies, the in-process
//! reads (the walk's signature, `directories_held`, the cuts, the root and
//! head leaves) and the monotonic clock, from a step's fields, or drawn
//! from a seeded splitmix64 as each is asked for.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;
use std::sync::atomic::AtomicI64;

use bettermemory_core::fsdata::realpath::Unresolved;
use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::git_run::{GitProcess, Spawned};
use bettermemory_core::gitconfig::paths::Machine;
use bettermemory_core::gitconfig::remotes::{Inputs, Sources};
use bettermemory_core::gitconfig::stamped::StampedReads;
use bettermemory_core::gitfile_rule::discovery::{Reading, Rule};
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::gitfile_rule::trust::Refusal;
use bettermemory_core::githead::discovery::{Env as HeadEnv, Found, Host};
use bettermemory_core::githead::GitDir;
use bettermemory_core::gitsystem::ask::{Env, PINS};
use bettermemory_core::origin_dates::OriginGit;
use bettermemory_core::origin_git::{self, Context, Counts, Files, OriginRunner};
use bettermemory_core::origin_gitfiles::attrs::IndexMemo;
use bettermemory_core::origin_gitfiles::facts::Held;
use bettermemory_core::origin_gitfiles::root::{ConfigMemo, NotADirectorySeen};
use bettermemory_core::origin_gitfiles::walk::WalkFiles;
use bettermemory_core::origin_history::{
    Cut, ReachableWalk, Unwalked, Walked, AS_STORED_CONFIG, DEVNULL,
};
use bettermemory_core::origin_reach::{
    commits_since_anchor, walk_args, Error, Kept, Reads, RepositoryReads, WalkKey, WalkMemo,
    WalkReads,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::{cps, to_string};
use serde_json::{json, Value};

pub const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
pub const B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
pub const C: &str = "cccccccccccccccccccccccccccccccccccccccc";
pub const D: &str = "dddddddddddddddddddddddddddddddddddddddd";

pub fn text(s: &[u32]) -> String {
    to_string(s).unwrap()
}

fn bytes(s: &[u8]) -> String {
    String::from_utf8(s.to_vec()).unwrap()
}

/// `gen_origin_reach._Mix`: splitmix64, each draw reduced below `n`.
pub struct Mix(u64);

impl Mix {
    pub fn new(seed: u64) -> Mix {
        Mix(seed)
    }
    pub fn below(&mut self, n: u64) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        (z ^ (z >> 31)) % n
    }
}

/// The step under way, what it has asked so far, and the mix of a sweep.
#[derive(Default)]
pub struct World {
    pub step: Value,
    pub script: VecDeque<Value>,
    pub calls: Vec<Value>,
    pub reads: Vec<String>,
    pub sigs: usize,
    pub mix: Option<Mix>,
}

pub type Shared = Rc<RefCell<World>>;

impl World {
    pub fn begin(&mut self, step: &Value) {
        self.step = step.clone();
        self.script = step["script"].as_array().unwrap().clone().into();
        self.calls.clear();
        self.reads.clear();
        self.sigs = 0;
    }

    fn leaf(&mut self, name: &str, arg: &[u8]) -> Value {
        self.reads.push(format!("{name} {}", bytes(arg)));
        self.step["leaves"][name].clone()
    }

    fn mix(&mut self) -> Option<&mut Mix> {
        self.mix.as_mut()
    }
}

fn reply(code: i64, stdout: &str, stderr: &str) -> Value {
    json!([code, stdout, stderr])
}

/// `_WalkWorld.drawn`: the reply a sweep draws for `argv`.
fn drawn(mix: &mut Mix, argv: &[String]) -> Value {
    let k = mix.below(20);
    if !argv.iter().any(|a| a == "log") {
        if k < 15 {
            return [0, 0, 1, 128, -9].map(|c| reply(c, "", ""))[(k % 5) as usize].clone();
        }
        return json!(["timeout", "timeout", "missing", "oserror", "timeout"][(k - 15) as usize]);
    }
    let range = argv.last().unwrap();
    let (anchor, head) = range.split_once("..").unwrap();
    let names = argv.iter().any(|a| a == "--name-only");
    match k {
        0..=9 => ok(anchor, head, names),
        10..=13 => json!("timeout"),
        14..=16 => reply(128, "", "fatal: bad revision\n"),
        17 if names => reply(0, &format!("\x01>{}\n\x01-{anchor}\n", &head[..39]), ""),
        17 => json!("oserror"),
        18 => json!("missing"),
        _ => reply(0, &format!("\x01>{head}\n\x01-{D}\n"), ""),
    }
}

/// `_ok`: a walk git lists.
pub fn ok(anchor: &str, head: &str, names: bool) -> Value {
    if names {
        reply(0, &format!("\x01>{head}\n\nf.txt\n\x01-{anchor}\n"), "")
    } else {
        reply(0, &format!("\x01>{head}\n\x01-{anchor}\n"), "")
    }
}

/// The scripted git.
pub struct Process(pub Shared);

impl GitProcess for Process {
    fn run(
        &mut self,
        argv: &[PyStr],
        cwd: Option<&[u32]>,
        _env: &Env,
        stdin: Option<&[u8]>,
        timeout: f64,
    ) -> Spawned {
        assert!(stdin.is_none());
        let prefix: Vec<_> = ["git"]
            .into_iter()
            .chain(PINS)
            .chain(AS_STORED_CONFIG)
            .map(cps)
            .collect();
        let rest = argv
            .strip_prefix(prefix.as_slice())
            .expect("pins and config");
        let null = format!("-O{DEVNULL}");
        let words: Vec<String> = rest
            .iter()
            .map(|a| match text(a) {
                a if a == null => "-O<devnull>".to_owned(),
                a => a,
            })
            .collect();
        let mut call = vec![json!(timeout), json!(text(cwd.unwrap()))];
        if words.iter().any(|a| a == "log") {
            let range = words.last().unwrap().clone();
            let (anchor, head) = range.split_once("..").unwrap();
            let names = words.iter().any(|a| a == "--name-only");
            assert_eq!(rest, walk_args(&cps(anchor), &cps(head), names).as_slice());
            call.extend([json!(if names { "names" } else { "listing" }), json!(range)]);
        } else {
            call.extend(words.iter().map(|w| json!(w)));
        }
        let mut world = self.0.borrow_mut();
        world.calls.push(json!(call));
        let answer = match world.mix() {
            Some(mix) => drawn(mix, &words),
            None => world.script.pop_front().expect("a scripted git"),
        };
        match answer.as_str() {
            Some("timeout") => Spawned::TimedOut,
            Some("missing") => Spawned::NotFound(cps("no executable")),
            Some("oserror") => Spawned::OsError(cps("scripted")),
            Some(other) => panic!("unknown reply {other}"),
            None => Spawned::Exited {
                returncode: answer[0].as_i64().unwrap(),
                stdout: answer[1].as_str().unwrap().as_bytes().to_vec(),
                stderr: answer[2].as_str().unwrap().as_bytes().to_vec(),
            },
        }
    }
}

/// The runner's file reads: the refusal, and `toplevel_agrees` true.
pub struct Leaves(pub Shared);

impl Files for Leaves {
    fn reading(&mut self, _: &[u8]) -> Result<Option<Reading>, origin_git::Error> {
        let refuse = self.0.borrow().step["leaves"]["refusal"] == true;
        Ok(refuse.then(|| Reading {
            why: Some(Refusal {
                why: b"separate checkout".to_vec(),
                format_unknown: false,
            }),
            ..Reading::default()
        }))
    }
    fn stat_dir(&self, _: &[u8]) -> Result<bool, Failure> {
        Ok(true)
    }
    fn refused_root(&mut self, _: &[u8]) -> Result<(Option<Vec<u8>>, bool), origin_git::Error> {
        panic!("unexpected refused root")
    }
    fn agrees(&mut self, _: &[u8], _: &[u8]) -> Result<bool, origin_git::Error> {
        Ok(true)
    }
    fn resolve(&mut self, _: &[u8]) -> Result<Vec<u8>, Unresolved> {
        panic!("unexpected runner resolution")
    }
}

/// The walk files a token stands for: the config's stamp numbered.
pub fn files_of(token: &str) -> WalkFiles {
    let n: i128 = token[1..].parse().unwrap();
    WalkFiles {
        config: Some([n, 0, 0, 0, 0]),
        worktree_config: None,
        gitmodules: None,
        index: None,
        shallow: None,
    }
}

pub fn token_of(files: &WalkFiles) -> String {
    format!("s{}", files.config.unwrap()[0])
}

/// The scripted reads, as the generator patches them.
pub struct Scripted(pub Shared);

impl Reads for Scripted {
    fn flavour(&self) -> Flavour {
        Flavour::Posix
    }
    fn find_gitdir(&mut self, cwd: &[u8]) -> Option<Found> {
        let found = self.0.borrow_mut().leaf("find_gitdir", cwd) == true;
        found.then(|| Found {
            gd: GitDir {
                gitdir: b"gd".to_vec(),
                commondir: b"cd".to_vec(),
            },
            worktree_root: b"wt".to_vec(),
        })
    }
    fn head_sha(&mut self, gd: &GitDir) -> Option<String> {
        let head = self.0.borrow_mut().leaf("head_sha", &gd.gitdir);
        head.as_str().map(str::to_owned)
    }
    fn config_moves_worktree(&mut self, gd: &GitDir) -> Result<bool, Error> {
        Ok(self.0.borrow_mut().leaf("moves", &gd.gitdir) == true)
    }
    fn machine_config_dies(&mut self, git_dir: &[u8]) -> Result<bool, Error> {
        Ok(self.0.borrow_mut().leaf("dies", git_dir) == true)
    }
    fn spelled_as_git(&mut self, root: &[u8]) -> Result<Option<Vec<u8>>, Error> {
        let spelled = self.0.borrow_mut().leaf("spelled", root);
        Ok(spelled.as_str().map(|s| s.as_bytes().to_vec()))
    }
    fn resolve(&mut self, path: &[u8]) -> Result<Vec<u8>, Unresolved> {
        self.0.borrow_mut().leaf("resolve", path);
        Ok([path, b"~r"].concat())
    }
    fn cuts(&mut self, toplevel: &[u8]) -> Result<Option<Cut>, Error> {
        let mut world = self.0.borrow_mut();
        world.reads.push(format!("cuts {}", bytes(toplevel)));
        if let Some(mix) = world.mix() {
            return Ok((mix.below(10) != 0).then(Cut::new));
        }
        Ok(world.step["cuts"]
            .as_array()
            .map(|shas| shas.iter().map(|s| cps(s.as_str().unwrap())).collect()))
    }
    fn walk_files(
        &mut self,
        root: &[u8],
        held: Option<&mut Held>,
    ) -> Result<Option<WalkFiles>, Error> {
        let mut world = self.0.borrow_mut();
        let mark = if held.is_some() { "sig+" } else { "sig" };
        world.reads.push(format!("{mark} {}", bytes(root)));
        let first = world.sigs == 0;
        let token = match world.mix() {
            Some(mix) => match mix.below(10) {
                0..=6 => json!("s1"),
                7..=8 => json!("s2"),
                _ => Value::Null,
            },
            None => {
                let step = world.step.as_object().unwrap();
                match step.get("after") {
                    Some(after) if !first => after.clone(),
                    _ => step["sig"].clone(),
                }
            }
        };
        world.sigs += 1;
        Ok(token.as_str().map(files_of))
    }
    fn directories_held(&mut self, _held: &Held) -> Result<bool, Error> {
        let mut world = self.0.borrow_mut();
        world.reads.push("held".to_owned());
        Ok(match world.mix() {
            Some(mix) => mix.below(8) != 0,
            None => world.step["held"] == true,
        })
    }
    fn monotonic(&mut self) -> Result<f64, Error> {
        let mut world = self.0.borrow_mut();
        world.reads.push("clock".to_owned());
        Ok(world.step["now"].as_f64().unwrap())
    }
}

/// `_walk_answer`.
pub fn walk_answer(walk: Option<&ReachableWalk>) -> Value {
    let Some(walk) = walk else { return Value::Null };
    let touched: Vec<Value> = walk
        .touched
        .iter()
        .map(|(p, shas)| json!([text(p), shas.iter().map(|s| text(s)).collect::<Vec<_>>()]))
        .collect();
    json!([
        walk.commits.iter().map(|s| text(s)).collect::<Vec<_>>(),
        touched,
        walk.listed,
        walk.timed_out
    ])
}

fn dump(entries: Vec<Value>) -> Value {
    let n = entries.len();
    let mut out = vec![json!(n)];
    if n <= 4 {
        out.extend(entries);
    } else {
        out.push(entries[0].clone());
        out.push(entries[n - 1].clone());
    }
    json!(out)
}

fn key_json(k: &WalkKey) -> Vec<Value> {
    vec![
        json!(text(&k.root)),
        json!(text(&k.anchor)),
        json!(text(&k.head)),
        json!(token_of(&k.files)),
    ]
}

/// `_memos_now`.
pub fn memos_now(memo: &WalkMemo) -> Value {
    let keys = memo
        .memo_keys()
        .iter()
        .map(|k| json!(key_json(k)))
        .collect();
    let kept = memo
        .timed_out_entries()
        .iter()
        .map(|(k, stamp, kept)| {
            let mut entry = key_json(k);
            entry.push(json!(stamp));
            entry.push(json!(match kept {
                Kept::Walk(_) => "walk",
                Kept::Unread(why) => why.value(),
            }));
            json!(entry)
        })
        .collect();
    json!([dump(keys), dump(kept)])
}

/// `_given_up`: the walks given up on and why the last was.
pub fn given_up(unwalked: &Unwalked) -> Value {
    let why = unwalked
        .given_up_since(unwalked.count().wrapping_add(1))
        .unwrap();
    json!([unwalked.count(), why.value()])
}

/// One row's or one sweep sequence's state: a fresh memo, runner tallies
/// and pass.
pub struct Run {
    pub world: Shared,
    pub memo: WalkMemo,
    pub counts: Counts,
    pub global: AtomicI64,
    pub unwalked: Unwalked,
    pub walked: Walked,
}

impl Run {
    pub fn new(world: Shared) -> Run {
        Run {
            world,
            memo: WalkMemo::new(),
            counts: Counts::default(),
            global: AtomicI64::new(0),
            unwalked: Unwalked::new(),
            walked: Walked::new(),
        }
    }

    /// `_since`: one call of `commits_since_anchor` over the step (its
    /// defaults merged), and the five tallies after it.
    pub fn since(&mut self, step: &Value) -> (Result<Option<ReachableWalk>, Error>, Value) {
        self.world.borrow_mut().begin(step);
        let (scope, env) = (Scope::default(), Env::new());
        let mut seen = NotADirectorySeen::new();
        let mut process = Process(self.world.clone());
        let mut files = Leaves(self.world.clone());
        let mut reads = Scripted(self.world.clone());
        let mut runner = OriginRunner::new(
            Context {
                flavour: Flavour::Posix,
                scope: &scope,
                clock: &|| 0.,
                env: &env,
                global_unanswered: &self.global,
                first_not_a_directory: &mut seen,
            },
            &mut process,
            &mut files,
            &mut self.counts,
        );
        let cwd = step["cwd"].as_str().map(str::as_bytes);
        let anchor = cps(step["anchor"].as_str().unwrap());
        // `toplevel_cps` where the root holds a surrogate.
        let toplevel = match step.get("toplevel_cps") {
            Some(points) => Some(super::cps_of(points)),
            None => step["toplevel"].as_str().map(cps),
        };
        let head = step["head"].as_str().map(cps);
        let walked = (step["walked"] == true).then_some(&mut self.walked);
        let answer = commits_since_anchor(
            &self.memo,
            &mut runner,
            &mut reads,
            &mut self.unwalked,
            cwd,
            &anchor,
            (toplevel.as_deref(), head.as_deref()),
            walked,
        );
        let (timed, missing) = runner.counts();
        let totals = json!([
            runner.unanswered_git_calls(),
            runner.failed_git_calls(),
            timed,
            missing,
            runner.unanswered()
        ]);
        (answer.map(|w| w.map(|w| (*w).clone())), totals)
    }
}

/// A step with the header's defaults under it.
pub fn merged(defaults: &Value, step: &Value) -> Value {
    let mut out = defaults.as_object().unwrap().clone();
    for (k, v) in step.as_object().unwrap() {
        out.insert(k.clone(), v.clone());
    }
    Value::Object(out)
}

/// `_walk_row`'s outputs for one row's steps.
pub fn run_row(defaults: &Value, steps: &[Value]) -> Vec<Value> {
    let mut run = Run::new(Shared::default());
    let mut out = Vec::new();
    for given in steps {
        let step = merged(defaults, given);
        if let Some(fill) = step.get("fill") {
            let (start, count) = (fill[0].as_u64().unwrap(), fill[1].as_u64().unwrap());
            let (mut runs, mut totals) = (0, Value::Null);
            for i in start..start + count {
                let anchor = format!("{i:040x}");
                let script = match fill[2].as_str().unwrap() {
                    "walk" => json!([ok(&anchor, C, true)]),
                    _ => json!(["timeout", "timeout"]),
                };
                let one = merged(&step, &json!({"anchor": anchor, "script": script}));
                let (answer, after) = run.since(&one);
                answer.unwrap();
                totals = after;
                let world = run.world.borrow();
                assert!(world.script.is_empty());
                runs += world.calls.len();
            }
            out.push(json!({
                "counts": totals,
                "unwalked": given_up(&run.unwalked),
                "runs": runs,
                "memos": memos_now(&run.memo),
            }));
            continue;
        }
        let (answer, totals) = run.since(&step);
        let answer = answer.unwrap();
        let world = run.world.borrow();
        assert!(world.script.is_empty(), "{step}");
        out.push(json!({
            "result": walk_answer(answer.as_ref()),
            "counts": totals,
            "unwalked": given_up(&run.unwalked),
            "calls": world.calls,
            "reads": world.reads,
            "memos": memos_now(&run.memo),
        }));
    }
    out
}

/// `gen_origin_gitfiles_attrs.RELEASE`.
const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

struct NoGit;

impl GitProcess for NoGit {
    fn run(
        &mut self,
        _: &[PyStr],
        _: Option<&[u32]>,
        _: &Env,
        _: Option<&[u8]>,
        _: f64,
    ) -> Spawned {
        panic!("no git runs where the signature declines")
    }
}

/// The Windows flavour declines `walk_files_signature` (`NotPorted`), so
/// `commits_since_anchor` declines before the walk runs, and before any
/// git where the caller passes the root and the head, as here (otherwise
/// `repo_toplevel_and_head` runs first); GIT_INDEX_FILE set answers None
/// first, as the reference does.
pub fn windows_flavour_declines() {
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Windows,
        cwd: b"C:\\",
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &StdFs,
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let host = Host {
        euid: 0,
        owners: &StdFs,
    };
    let machine = Machine {
        flavour: Flavour::Windows,
        home: None,
        profile: None,
        user_home: &|_| None,
    };
    let inputs = Inputs {
        machine: &machine,
        env: Default::default(),
        sources: Sources::default(),
        git: GIT,
        cwd: b"C:\\".to_vec(),
        asked_branch: None,
    };
    let head_env = HeadEnv::default();
    let (mut config, mut index, mut stamped) =
        (ConfigMemo::new(), IndexMemo::new(), StampedReads::new());
    let mut clock = || 0.;
    let mut repository = RepositoryReads {
        rule: &rule,
        host: &host,
        env: &head_env,
        config_memo: &mut config,
        machine_reads: &mut stamped,
        inputs: &inputs,
        clock: &mut clock,
        darwin: false,
    };
    let mut reads = WalkReads {
        reads: &mut repository,
        index_memo: &mut index,
        index_file_set: false,
    };
    let mut held = Held::new();
    assert_eq!(
        reads.walk_files(b"C:\\r", Some(&mut held)),
        Err(Error::NotPorted)
    );
    assert!(held.is_empty());
    let (scope, env, global) = (Scope::default(), Env::new(), AtomicI64::new(0));
    let mut seen = NotADirectorySeen::new();
    let mut counts = Counts::default();
    let (mut process, mut files) = (NoGit, Leaves(Shared::default()));
    let mut runner = OriginRunner::new(
        Context {
            flavour: Flavour::Windows,
            scope: &scope,
            clock: &|| 0.,
            env: &env,
            global_unanswered: &global,
            first_not_a_directory: &mut seen,
        },
        &mut process,
        &mut files,
        &mut counts,
    );
    let memo = WalkMemo::new();
    let mut unwalked = Unwalked::new();
    let found = commits_since_anchor(
        &memo,
        &mut runner,
        &mut reads,
        &mut unwalked,
        Some(b"C:\\r"),
        &cps(&"a".repeat(40)),
        (Some(&cps("C:\\r")), Some(&cps(C))),
        None,
    );
    assert_eq!(found, Err(Error::NotPorted));
    // GIT_INDEX_FILE set: the reference's None, read before the flavour.
    reads.index_file_set = true;
    assert_eq!(reads.walk_files(b"C:\\r", None), Ok(None));
}
