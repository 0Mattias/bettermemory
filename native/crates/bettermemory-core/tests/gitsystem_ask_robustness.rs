//! The ask over inputs no fixture names, and its cost.
//!
//! - Random environments, scripts and clocks: the ask never panics, and
//!   every command it hands git carries the pins, `GIT_OPTIONAL_LOCKS=0`
//!   and `NO_FETCH`, and none of `SCRUBBED_ENV` or `RESHAPING_ENV`, nor
//!   (in the probe) `_PROBE_UNSET`'s config variables but the run's own.
//! - Output past `OUTPUT_LIMIT` is taken as no answer (the stated
//!   divergence: the reference reads it all).
//! - `environment` and `which` are linear: ten times the variables or the
//!   PATH entries cost under thirty times the time.

use std::time::{Duration, Instant};

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitsystem::ask::{
    environment, which, Env, NO_FETCH, PINS, RESHAPING_ENV, SCRUBBED_ENV,
};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// xorshift64*.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
}

const NAMES: [&str; 24] = [
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_SHALLOW_FILE",
    "GIT_INDEX_FILE",
    "GIT_DIFF_OPTS",
    "GIT_ATTR_SOURCE",
    "GIT_EXTERNAL_DIFF",
    "GIT_DIFF_PATH_COUNTER",
    "GIT_DIFF_PATH_TOTAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OPTIONAL_LOCKS",
    "GIT_NO_LAZY_FETCH",
    "GIT_ALLOW_PROTOCOL",
    "GIT_CEILING_DIRECTORIES",
    "git_dir",
    "PATH",
    "X",
];

fn random_env(rng: &mut Rng) -> Env {
    let mut env: Env = Vec::new();
    for _ in 0..rng.below(30) {
        let k = cps(NAMES[rng.below(NAMES.len())]);
        if env.iter().any(|(n, _)| *n == k) {
            continue;
        }
        let v = cps(["", "1", "0", "/x", "ext", "@"][rng.below(6)]);
        env.push((k, v));
    }
    env
}

/// What every command's environment must hold, whatever the caller's.
fn check(env: &Env, extra: &[(Vec<u32>, Vec<u32>)], unset: &[&str], got: &Env) {
    let get = |n: &str| {
        got.iter()
            .find(|(k, _)| *k == cps(n))
            .map(|(_, v)| v.clone())
    };
    assert_eq!(get("GIT_OPTIONAL_LOCKS"), Some(cps("0")));
    for (k, v) in NO_FETCH {
        assert_eq!(get(k), Some(cps(v)));
    }
    for n in SCRUBBED_ENV.iter().chain(RESHAPING_ENV.iter()) {
        assert_eq!(get(n), None, "{n} reached git");
    }
    for n in unset {
        let given = extra
            .iter()
            .find(|(k, _)| *k == cps(n))
            .map(|(_, v)| v.clone());
        assert_eq!(get(n), given, "{n} unset");
    }
    let mut names: Vec<&Vec<u32>> = got.iter().map(|(k, _)| k).collect();
    names.sort();
    let before = names.len();
    names.dedup();
    assert_eq!(names.len(), before, "a name twice");
    // Every other variable of the caller's passes as given.
    for (k, v) in env {
        let name: String = k.iter().map(|&c| char::from_u32(c).unwrap()).collect();
        let fixed = SCRUBBED_ENV.contains(&name.as_str())
            || RESHAPING_ENV.contains(&name.as_str())
            || unset.contains(&name.as_str())
            || name == "GIT_OPTIONAL_LOCKS"
            || NO_FETCH.iter().any(|(n, _)| *n == name)
            || extra.iter().any(|(e, _)| e == k);
        if !fixed {
            assert_eq!(get(&name), Some(v.clone()), "{name} changed");
        }
    }
}

#[test]
fn random_environments_keep_the_scrub() {
    let mut rng = Rng(0x9e37_79b9_7f4a_7c15);
    let unsets: [&[&str]; 3] = [
        &["GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"],
        &[
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_COUNT",
        ],
        &[],
    ];
    for _ in 0..20_000 {
        let env = random_env(&mut rng);
        let extra: Vec<(Vec<u32>, Vec<u32>)> = random_env(&mut rng)
            .into_iter()
            .take(rng.below(4))
            .collect();
        let unset = unsets[rng.below(3)];
        let got = environment(&env, &extra, unset);
        check(&env, &extra, unset, &got);
    }
    assert_eq!(PINS.len(), 8);
}

fn timed(f: &dyn Fn()) -> Duration {
    (0..3)
        .map(|_| {
            let s = Instant::now();
            f();
            s.elapsed()
        })
        .min()
        .unwrap()
}

fn linear(name: &str, run: &dyn Fn(usize)) {
    let small = timed(&|| run(2_000));
    let large = timed(&|| run(20_000));
    assert!(
        large < small * 30 + Duration::from_millis(50),
        "{name}: 2,000 {small:?}, 20,000 {large:?}"
    );
}

#[test]
fn the_environment_and_which_are_linear() {
    linear("environment", &|n| {
        let env: Env = (0..n).map(|i| (cps(&format!("V{i}")), cps("x"))).collect();
        let got = environment(
            &env,
            &[(cps("GIT_CONFIG_GLOBAL"), cps("/dev/null"))],
            &["V1"],
        );
        assert_eq!(got.len(), n + 3);
    });
    for flavour in [Flavour::Posix, Flavour::Windows] {
        linear("which", &|n| {
            let mut fs = MemFs::new(flavour);
            let (root, sep) = match flavour {
                Flavour::Posix => ("/b", ":"),
                Flavour::Windows => ("C:\\b", ";"),
            };
            fs.insert(format!("{root}{n}").as_bytes(), Node::Dir);
            let path: Vec<String> = (0..n).map(|i| format!("{root}{i}")).collect();
            let env: Env = vec![(cps("PATH"), cps(&path.join(sep)))];
            assert_eq!(which(&fs, flavour, b"/", &env, None), None);
        });
    }
}

#[cfg(unix)]
mod disk {
    use super::*;
    use bettermemory_core::fsdata::StdFs;
    use bettermemory_core::gitsystem::ask::{
        Asker, Budget, Clock, Command, Git, Ran, OUTPUT_LIMIT, PROBE_UNSET,
    };
    use bettermemory_core::gitsystem::record::Place;
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};

    struct Scratch(PathBuf);

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn scratch() -> Scratch {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("bm-gitsysask-rob-{}-{n}", std::process::id()));
        std::fs::create_dir(&dir).unwrap();
        Scratch(std::fs::canonicalize(&dir).unwrap())
    }

    /// A git whose answers are random, checking each command.
    struct Random<'e> {
        rng: Rng,
        env: &'e Env,
        commands: usize,
    }

    impl Git for Random<'_> {
        fn run(&mut self, c: &Command) -> Ran {
            self.commands += 1;
            let pins: Vec<Vec<u32>> = PINS.iter().map(|p| cps(p)).collect();
            assert_eq!(c.argv[0], cps("git"));
            assert_eq!(&c.argv[1..9], &pins[..], "the pins");
            assert_eq!(c.limit, OUTPUT_LIMIT);
            let probe = c.argv.iter().any(|a| a.starts_with(&cps("--git-dir=")));
            let unset: &[&str] = if probe {
                &PROBE_UNSET
            } else {
                &["GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"]
            };
            let extra: Vec<(Vec<u32>, Vec<u32>)> = c
                .env
                .iter()
                .filter(|(k, _)| {
                    [
                        "GIT_CONFIG_SYSTEM",
                        "GIT_CONFIG_GLOBAL",
                        "GIT_CONFIG_NOSYSTEM",
                        "GIT_CEILING_DIRECTORIES",
                    ]
                    .iter()
                    .any(|n| *k == cps(n))
                })
                .cloned()
                .collect();
            check(self.env, &extra, unset, &c.env);
            match self.rng.below(5) {
                0 => Ran::NotRun,
                1 => Ran::TimedOut,
                _ => {
                    let n = self.rng.below(40);
                    let stdout = (0..n)
                        .map(|_| [0u8, b'\n', b'a', 0xff, b'/'][self.rng.below(5)])
                        .collect();
                    Ran::Exited {
                        status: [0, 0, 1, 128][self.rng.below(4)],
                        stdout,
                    }
                }
            }
        }
    }

    struct Jumpy(Rng, f64);

    impl Clock for Jumpy {
        fn monotonic(&mut self) -> f64 {
            self.1 += [0.0, 0.01, 0.3, 10.0][self.0.below(4)];
            self.1
        }
        fn wall(&mut self) -> f64 {
            2e9
        }
    }

    fn tree(root: &Path) {
        std::fs::create_dir(root.join("bin")).unwrap();
        std::fs::write(root.join("bin/git"), b"#!").unwrap();
        std::fs::set_permissions(root.join("bin/git"), std::fs::Permissions::from_mode(0o755))
            .unwrap();
    }

    #[test]
    fn random_asks_never_panic_and_keep_the_scrub() {
        let base = scratch();
        let mut rng = Rng(0x1234_5678_9abc_def1);
        for i in 0..400 {
            let root = base.0.join(i.to_string());
            std::fs::create_dir(&root).unwrap();
            tree(&root);
            let mut env = random_env(&mut rng);
            env.retain(|(k, _)| *k != cps("PATH"));
            env.push((cps("PATH"), cps(&format!("{}/bin", root.display()))));
            let state = format!("{}/state", root.display());
            let cwd = root.to_str().unwrap().to_owned();
            let place = Place {
                fs: &StdFs,
                flavour: Flavour::Posix,
                state_dir: state.as_bytes(),
                cwd: cwd.as_bytes(),
            };
            let mut git = Random {
                rng: Rng(rng.next() | 1),
                env: &env,
                commands: 0,
            };
            let mut clock = Jumpy(Rng(rng.next() | 1), 100.0);
            let mut budget = match rng.below(3) {
                0 => Budget::Inactive,
                1 => Budget::Active { deadline: None },
                _ => Budget::Active {
                    deadline: Some(100.0 + rng.below(20) as f64 * 0.05),
                },
            };
            let _ = Asker {
                place: &place,
                git: &mut git,
                clock: &mut clock,
                env: &env,
                cs_path: None,
                budget: &mut budget,
            }
            .ask("k", 1.0);
            assert!(git.commands <= 7);
        }
    }

    #[test]
    fn output_past_the_limit_is_no_answer() {
        let base = scratch();
        tree(&base.0);
        let env: Env = vec![(cps("PATH"), cps(&format!("{}/bin", base.0.display())))];
        let state = format!("{}/state", base.0.display());
        let cwd = base.0.to_str().unwrap().to_owned();
        let place = Place {
            fs: &StdFs,
            flavour: Flavour::Posix,
            state_dir: state.as_bytes(),
            cwd: cwd.as_bytes(),
        };
        struct Big;
        impl Git for Big {
            fn run(&mut self, c: &Command) -> Ran {
                Ran::Exited {
                    status: 0,
                    stdout: vec![b'/'; c.limit + 1],
                }
            }
        }
        struct Still(f64);
        impl Clock for Still {
            fn monotonic(&mut self) -> f64 {
                self.0
            }
            fn wall(&mut self) -> f64 {
                2e9
            }
        }
        let got = Asker {
            place: &place,
            git: &mut Big,
            clock: &mut Still(100.0),
            env: &env,
            cs_path: None,
            budget: &mut Budget::Inactive,
        }
        .ask("k", 1.0)
        .unwrap();
        assert!(!got.ok, "an answer past the limit is none");
    }
}
