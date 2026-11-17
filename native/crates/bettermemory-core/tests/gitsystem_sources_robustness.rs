//! `sources` over sequences no fixture names, and its cost.
//!
//! - Random sequences of calls over one held cache (PATH, DEVELOPER_DIR
//!   and git's binary changing, the cache cleared, the clock jumping both
//!   ways, git answering, failing or saying part): no answer stands for a
//!   git, PATH or developer directory other than the one it was asked of.
//!   An answer reused without asking is one whose git stamps as it did,
//!   found on the PATH now, unless it is kept after a failed ask
//!   (`tried`), which the reference keeps for `RETRY_SECONDS` whatever git
//!   does meanwhile.
//! - Under the Windows flavour `sources` declines and holds nothing.
//! - The key is linear in the environment: ten times the variables cost
//!   under thirty times the time.

use std::time::{Duration, Instant};

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitsystem::ask::{Budget, Clock, Command, Declined, Env, Git, Ran};
use bettermemory_core::gitsystem::record::Place;
use bettermemory_core::gitsystem::sources::{Caller, Fault, Held};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// How git answers: every question, none, or all but the version. Only
/// the POSIX module's sequences make the last two.
#[derive(Clone, Copy)]
#[cfg_attr(not(unix), allow(dead_code))]
enum Mood {
    Whole,
    Fails,
    NoVersion,
}

struct Scripted {
    mood: Mood,
    asked: usize,
}

impl Git for Scripted {
    fn run(&mut self, c: &Command) -> Ran {
        self.asked += 1;
        if let Mood::Fails = self.mood {
            return Ran::NotRun;
        }
        let last: String = c
            .argv
            .last()
            .unwrap()
            .iter()
            .map(|&x| char::from_u32(x).unwrap())
            .collect();
        let stdout: &[u8] = match last.as_str() {
            "GIT_CONFIG_SYSTEM" => b"/etc/gitconfig\n",
            "-z" => b"system\0file:/etc/gitconfig\0a\nb\0",
            "version" => match self.mood {
                Mood::NoVersion => b"version 2\n",
                _ => b"git version 2.54.0\n",
            },
            "p" => b"bettermemory-probe-b\n",
            r"^bettermemory\." => b"bettermemory.prefix\n/usr/z\0bettermemory.hasconfig\nyes\0",
            _ => b".\n",
        };
        Ran::Exited {
            status: 0,
            stdout: stdout.to_vec(),
        }
    }
}

struct Clocks {
    mono: f64,
    wall: f64,
}

impl Clock for Clocks {
    fn monotonic(&mut self) -> f64 {
        self.mono += 0.001;
        self.mono
    }
    fn wall(&mut self) -> f64 {
        self.wall
    }
}

#[test]
fn under_windows_sources_declines_and_holds_nothing() {
    let fs = MemFs::new(Flavour::Windows);
    let place = Place {
        fs: &fs,
        flavour: Flavour::Windows,
        state_dir: b"C:\\s",
        cwd: b"C:\\w",
    };
    let env: Env = vec![(cps("Path"), cps("C:\\bin"))];
    let mut held = Held::new();
    let mut git = Scripted {
        mood: Mood::Whole,
        asked: 0,
    };
    let got = Caller {
        place: &place,
        git: &mut git,
        clock: &mut Clocks {
            mono: 100.0,
            wall: 1000.0,
        },
        env: &env,
        cs_path: None,
        budget: &mut Budget::Inactive,
        macos: false,
        link: b"C:\\x",
    }
    .sources(&mut held, true);
    assert_eq!(got, Err(Fault::Declined(Declined::NotPorted)));
    assert!(held.entries().is_empty());
    assert_eq!(git.asked, 0);
}

/// Under Windows a record is read before the ask declines: a standing one
/// is answered, one that does not stand is declined; nothing is written.
#[test]
fn under_windows_a_record_is_read_before_declining() {
    for (asked, stands) in [(1000.0, true), (1000.0 - 61.0, false)] {
        let mut fs = MemFs::new(Flavour::Windows);
        let env: Env = vec![(cps("Path"), cps("C:\\bin"))];
        let key = {
            let place = Place {
                fs: &fs,
                flavour: Flavour::Windows,
                state_dir: b"C:\\s",
                cwd: b"C:\\w",
            };
            let mut git = Scripted {
                mood: Mood::Whole,
                asked: 0,
            };
            Caller {
                place: &place,
                git: &mut git,
                clock: &mut Clocks {
                    mono: 0.0,
                    wall: 0.0,
                },
                env: &env,
                cs_path: None,
                budget: &mut Budget::Inactive,
                macos: false,
                link: b"C:\\x",
            }
            .key()
        };
        let record = format!(
            "{{\"format\": 5, \"entries\": [{{\"path\": \"{key}\", \"git\": null, \
             \"stamp\": null, \"asked\": {asked:?}, \"ok\": false, \"own\": [], \
             \"system\": null}}]}}\n"
        );
        fs.insert(b"C:\\s", Node::Dir);
        fs.insert(
            b"C:\\s\\git-config-sources-v5.json",
            Node::File(record.clone().into_bytes()),
        );
        let place = Place {
            fs: &fs,
            flavour: Flavour::Windows,
            state_dir: b"C:\\s",
            cwd: b"C:\\w",
        };
        let mut held = Held::new();
        let mut git = Scripted {
            mood: Mood::Whole,
            asked: 0,
        };
        let got = Caller {
            place: &place,
            git: &mut git,
            clock: &mut Clocks {
                mono: 100.0,
                wall: 1000.0,
            },
            env: &env,
            cs_path: None,
            budget: &mut Budget::Inactive,
            macos: false,
            link: b"C:\\x",
        }
        .sources(&mut held, true);
        if stands {
            assert_eq!(got.map(|s| s.system), Ok(None));
            assert_eq!(held.entries().len(), 1);
        } else {
            assert_eq!(got, Err(Fault::Declined(Declined::NotPorted)));
            assert!(held.entries().is_empty());
        }
        assert_eq!(git.asked, 0);
        // `Fs` writes nothing; `record` writes through `std::fs`, which on
        // this host would make a `C:\\s` beside the working directory.
        assert!(!std::path::Path::new("C:\\s").exists(), "something written");
    }
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

#[test]
fn the_key_is_linear_in_the_environment() {
    let run = |n: usize| {
        let fs = MemFs::new(Flavour::Posix);
        let place = Place {
            fs: &fs,
            flavour: Flavour::Posix,
            state_dir: b"/s",
            cwd: b"/",
        };
        let mut env: Env = (0..n).map(|i| (cps(&format!("V{i}")), cps("x"))).collect();
        env.push((cps("PATH"), cps("/bin")));
        env.push((cps("DEVELOPER_DIR"), cps("/d")));
        let mut git = Scripted {
            mood: Mood::Whole,
            asked: 0,
        };
        let caller = Caller {
            place: &place,
            git: &mut git,
            clock: &mut Clocks {
                mono: 0.0,
                wall: 0.0,
            },
            env: &env,
            cs_path: None,
            budget: &mut Budget::Inactive,
            macos: true,
            link: b"/x",
        };
        assert_eq!(caller.key().len(), 32);
    };
    let small = timed(&|| run(2_000));
    let large = timed(&|| run(20_000));
    assert!(
        large < small * 30 + Duration::from_millis(50),
        "key: 2,000 {small:?}, 20,000 {large:?}"
    );
}

#[cfg(unix)]
mod disk {
    use super::*;
    use bettermemory_core::fsdata::StdFs;
    use bettermemory_core::gitconfig::legacy::file_stamp;
    use bettermemory_core::gitsystem::ask::which;
    use bettermemory_core::gitsystem::record::{surrogatepass, KEPT};
    use std::collections::HashMap;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;

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

    /// What a key names: PATH, DEVELOPER_DIR and the link's target.
    type Named = (Vec<u32>, Option<Vec<u32>>, Option<Vec<u8>>);

    struct Scratch(PathBuf);

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn fresh_dir() -> PathBuf {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("bm-gitsyssrcrob-{}-{n}", std::process::id()));
        std::fs::create_dir(&dir).unwrap();
        std::fs::canonicalize(&dir).unwrap()
    }

    #[test]
    fn no_answer_stands_for_another_git_path_or_developer_directory() {
        let reask = bettermemory_core::gitsystem::record::REASK_SECONDS;
        let base = Scratch(fresh_dir());
        let mut rng = Rng(0x5eed_0005);
        let mut asks = 0;
        let mut reused = 0;
        for case in 0..60 {
            let root = base.0.join(case.to_string());
            for p in 0..3 {
                let dir = root.join(format!("p{p}"));
                std::fs::create_dir_all(&dir).unwrap();
                std::fs::write(dir.join("git"), b"#!").unwrap();
                std::fs::set_permissions(dir.join("git"), std::fs::Permissions::from_mode(0o755))
                    .unwrap();
            }
            let r = root.to_str().unwrap().to_owned();
            let state = format!("{r}/state");
            let link = format!("{r}/xcode");
            let place = Place {
                fs: &StdFs,
                flavour: Flavour::Posix,
                state_dir: state.as_bytes(),
                cwd: r.as_bytes(),
            };
            let mut held = Held::new();
            let mut now = 1000.0;
            let mut keys: HashMap<String, Named> = HashMap::new();
            for _ in 0..30 {
                let path = cps(&match rng.below(5) {
                    0 => format!("{r}/p0"),
                    1 => format!("{r}/p1"),
                    2 => format!("{r}/p1:{r}/p0"),
                    3 => format!("{r}/p2"),
                    _ => format!("{r}/nothing:{r}/p2"),
                });
                let mut env: Env = vec![(cps("PATH"), path.clone())];
                let dev = match rng.below(3) {
                    0 => None,
                    d => Some(cps(&format!("/dev{d}"))),
                };
                if let Some(d) = &dev {
                    env.push((cps("DEVELOPER_DIR"), d.clone()));
                }
                match rng.below(8) {
                    0 => {
                        let p = rng.below(3);
                        let git = root.join(format!("p{p}/git"));
                        let n = rng.below(1000);
                        std::fs::write(&git, format!("#!{n}")).unwrap();
                    }
                    1 => held.clear(),
                    2 => {
                        let _ = std::fs::remove_file(&link);
                        let t = format!("/x{}", rng.below(2));
                        std::os::unix::fs::symlink(t, &link).unwrap();
                    }
                    _ => {}
                }
                now += match rng.below(6) {
                    0 => -5.0,
                    1 => reask,
                    2 => 30.0,
                    _ => rng.below(1000) as f64,
                };
                let mood = match rng.below(4) {
                    0 => Mood::Fails,
                    1 => Mood::NoVersion,
                    _ => Mood::Whole,
                };
                let stamp = rng.below(4) != 0;
                let mut git = Scripted { mood, asked: 0 };
                let mut caller = Caller {
                    place: &place,
                    git: &mut git,
                    clock: &mut Clocks {
                        mono: 100.0,
                        wall: now,
                    },
                    env: &env,
                    cs_path: None,
                    budget: &mut Budget::Inactive,
                    macos: true,
                    link: link.as_bytes(),
                };
                let key = caller.key();
                let selected = std::fs::read_link(&link)
                    .ok()
                    .map(|p| p.into_os_string().into_encoded_bytes());
                let named = (path.clone(), dev.clone(), selected);
                // One key per PATH and developer directory.
                if let Some(before) = keys.insert(key.clone(), named.clone()) {
                    assert_eq!(
                        before, named,
                        "two PATHs or developer directories share a key"
                    );
                }
                let got = caller.sources(&mut held, stamp).unwrap();
                let asked = git.asked;
                asks += asked;
                let n = held.entries().len();
                assert!(n <= KEPT);
                let mut seen: Vec<&String> = held.entries().iter().map(|(k, _)| k).collect();
                seen.sort();
                seen.dedup();
                assert_eq!(seen.len(), n, "a PATH held twice");
                let a = held.get(&key).expect("the PATH's answer held");
                assert_eq!(a.sources, got);
                assert_eq!(a.key, key);
                if asked == 0 && a.tried.is_none() && a.ok {
                    reused += 1;
                    let found = which(&StdFs, Flavour::Posix, r.as_bytes(), &env, None);
                    assert_eq!(a.git, found, "an answer for a git not on this PATH");
                    if stamp {
                        let fresh = file_stamp(&StdFs, &surrogatepass(a.git.as_ref().unwrap()))
                            .unwrap()
                            .map(|f| f.map(|x| x.to_string()));
                        assert_eq!(fresh.as_ref(), a.stamp.as_ref(), "a stale git's answer");
                    }
                }
            }
        }
        assert!(asks > 1000 && reused > 100, "asks {asks}, reused {reused}");
    }
}
