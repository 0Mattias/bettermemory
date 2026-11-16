//! Hostile TOML and config files: deep nesting, a very long line, NUL
//! bytes, bytes that are not UTF-8, a 50 MB document. Every one returns a
//! value or an error, without a panic or a stack overflow, in time linear
//! in its length (each is timed, and the slowest bound is generous for an
//! unoptimised build on a slow runner).

use std::time::{Duration, Instant};

use bettermemory_core::config::{self, decode};
use bettermemory_core::toml::{self, Error, Value};

fn timed<T>(label: &str, limit: Duration, f: impl FnOnce() -> T) -> T {
    let start = Instant::now();
    let out = f();
    let took = start.elapsed();
    eprintln!("{label}: {took:?}");
    assert!(took < limit, "{label} took {took:?}");
    out
}

const LIMIT: Duration = Duration::from_secs(60);

#[test]
fn deep_nesting_is_refused_without_overflowing() {
    for doc in [
        format!("a = {}", "[".repeat(1_000_000)),
        format!("a = {}1{}", "{b = ".repeat(200_000), "}".repeat(200_000)),
        format!("a = {}", "[{b = ".repeat(300_000)),
        format!("a = {}{}", "[".repeat(497), "]".repeat(497)),
    ] {
        let result = timed("nesting", LIMIT, || toml::loads(&doc));
        if doc.len() < 2000 {
            assert!(result.is_ok(), "497 arrays load");
        } else {
            assert_eq!(result.unwrap_err(), Error::Recursion);
        }
    }
    let at_limit = format!("a = {}{}", "[".repeat(498), "]".repeat(498));
    assert_eq!(toml::loads(&at_limit).unwrap_err(), Error::Recursion);
    let tables = |n: usize| format!("a = {}1{}", "{b = ".repeat(n), "}".repeat(n));
    assert!(toml::loads(&tables(331)).is_ok());
    assert_eq!(toml::loads(&tables(332)).unwrap_err(), Error::Recursion);
}

#[test]
fn long_lines_nul_and_large_documents() {
    let long = format!("a = \"{}\"\n", "x".repeat(20_000_000));
    let table = timed("20 MB string", LIMIT, || toml::loads(&long).unwrap());
    assert!(matches!(table.get("a"), Some(Value::String(s)) if s.len() == 20_000_000));

    let nul = "\0".repeat(1_000_000);
    let err = timed("NUL", LIMIT, || toml::loads(&nul).unwrap_err());
    assert_eq!(err.message(), "Invalid statement (at line 1, column 1)");

    let comment = format!("# {}\na = 1\n", "\0".repeat(1_000_000));
    let err = timed("NUL in a comment", LIMIT, || {
        toml::loads(&comment).unwrap_err()
    });
    assert_eq!(
        err.message(),
        "Found invalid character '\\x00' (at line 1, column 3)"
    );

    let hex = format!("a = 0x{}\n", "f".repeat(10_000_000));
    let table = timed("10 MB hex", LIMIT, || toml::loads(&hex).unwrap());
    let Some(Value::Integer(i)) = table.get("a") else {
        panic!()
    };
    assert!(i.to_decimal().is_err(), "str() of it is refused");

    // About 50 MB: a million keys, a 20 MB array, a 10 MB multi-line string.
    let mut big = String::new();
    for i in 0..1_000_000 {
        big.push_str(&format!("k{i} = {i}\n"));
    }
    big.push_str("arr = [");
    big.push_str(&"1, ".repeat(6_000_000));
    big.push_str("]\ns = \"\"\"\n");
    big.push_str(&"line of text\n".repeat(800_000));
    big.push_str("\"\"\"\n");
    eprintln!("document: {} bytes", big.len());
    assert!(big.len() > 40_000_000);
    let table = timed("50 MB document", LIMIT, || toml::loads(&big).unwrap());
    assert_eq!(table.len(), 1_000_002);

    let mut many_ints = String::new();
    for i in 0..2_000 {
        many_ints.push_str(&format!("i{i} = {}\n", "7".repeat(4300)));
    }
    timed("2,000 ints of 4300 digits", LIMIT, || {
        toml::loads(&many_ints).unwrap()
    });
}

#[test]
fn bytes_that_are_not_utf8() {
    for raw in [
        &b"\xff\xfe\x00"[..],
        b"a = \"\xc0\xaf\"",
        b"\xef\xbb\xbf\xef\xbb\xbf",
        b"\xed\xa0\x80",
        b"\xf4\x90\x80\x80",
        b"a = 1\n\xe2\x82",
    ] {
        let _ = decode::decode_utf8_sig(raw);
    }
    // A second BOM is not dropped: it is the statement tomllib refuses.
    let text = decode::decode_utf8_sig(b"\xef\xbb\xbf\xef\xbb\xbfa = 1").unwrap();
    assert_eq!(
        toml::loads(&text).unwrap_err().message(),
        "Invalid statement (at line 1, column 1)"
    );
}

#[test]
fn hostile_values_through_the_loader() {
    use config::{Ctx, Guards, Source};
    let source = Source {
        text: "config.toml",
        resolved: "config.toml".into(),
        is_file: true,
    };
    for doc in [
        "[daemon]\nport = inf\n".to_owned(),
        "[daemon]\nport = nan\n".to_owned(),
        format!("[daemon]\nport = 0x{}\n", "f".repeat(100_000)),
        format!("[telemetry]\nenabled = 0x{}\n", "f".repeat(100_000)),
        format!(
            "[capture]\ncheckpoint_tokens = -0x{}\n",
            "f".repeat(100_000)
        ),
        format!(
            "[behavior]\nprompt_recall = \"{}\"\n",
            "x".repeat(1_000_000)
        ),
        format!("[{}]\nx = 1\n", "s".repeat(1_000_000)),
    ] {
        let Ok(mut data) = toml::loads(&doc) else {
            continue;
        };
        let mut guards = Guards::new();
        let mut ctx = Ctx::new(&mut guards);
        let _ = timed("loader", LIMIT, || {
            config::config_from_data(&mut data, &source, &mut ctx)
        });
    }
}

#[test]
fn hostile_files_through_the_reports() {
    use config::report::{self, SystemFs};
    use config::{Env, Guards, Locations, ManagedSource, World};
    let dir = std::env::temp_dir().join(format!("bm-config-robust-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let user = dir.join("config.toml");
    let fs = SystemFs {
        home: Some(dir.to_string_lossy().into_owned()),
        cwd: Some(dir.to_string_lossy().into_owned()),
    };
    let env = Env::new();
    let locations = Locations {
        user_config: user.to_string_lossy().into_owned(),
        managed: ManagedSource::File(dir.join("managed.toml").to_string_lossy().into_owned()),
        default_keys_dir: dir.join("keys").to_string_lossy().into_owned(),
        default_state_dir: dir.join("state").to_string_lossy().into_owned(),
        system_dirs: Vec::new(),
        flavour: if cfg!(windows) {
            config::Flavour::Windows
        } else {
            config::Flavour::Posix
        },
    };
    let world = World {
        fs: &fs,
        env: &env,
        locations: &locations,
    };
    let mut keys = String::new();
    for i in 0..30_000 {
        keys.push_str(
            &format!("[telemetry]\nkey{i} = 1\n")
                .replace("[telemetry]\n", if i == 0 { "[telemetry]\n" } else { "" }),
        );
    }
    let docs: Vec<Vec<u8>> = vec![
        format!("[{}]\nx = 1\n", "behavior".repeat(200_000)).into_bytes(),
        format!("[telemetry]\n{} = 1\n", "enabled".repeat(200_000)).into_bytes(),
        keys.into_bytes(),
        format!("a = {}\n", "[".repeat(100_000)).into_bytes(),
        b"\xff\xfe[telemetry]".to_vec(),
        vec![0u8; 1_000_000],
    ];
    for doc in docs {
        std::fs::write(&user, &doc).unwrap();
        let mut guards = Guards::new();
        let checked = timed("check", LIMIT, || report::check(&world, &mut guards));
        let shown = timed("show", LIMIT, || report::show(&world, &mut guards));
        if doc.starts_with(b"a = [") {
            assert_eq!(checked.unwrap_err().class, "RecursionError");
            assert_eq!(shown.unwrap_err().class, "RecursionError");
        } else {
            assert!(checked.is_ok() && shown.is_ok());
        }
    }
    std::fs::remove_dir_all(&dir).unwrap();
}

/// Values at the nesting budget (497 arrays, 331 inline tables) loaded,
/// reported by check, show, path and the audit, and dropped, all on a
/// thread with the default 2 MiB stack in an unoptimised build: nothing
/// the library returns is walked or dropped by unbounded recursion on the
/// caller's stack. (The reference's CLI stops with a RecursionError from
/// 494 arrays and 329 inline tables; these load natively.)
#[test]
fn values_at_the_budget_on_a_small_stack() {
    use config::report::{self, SystemFs};
    use config::{Env, Guards, Locations, ManagedSource, World};
    let worker = std::thread::Builder::new()
        .stack_size(2 * 1024 * 1024)
        .spawn(|| {
            let arrays = format!("{}{}", "[".repeat(497), "]".repeat(497));
            let tables = format!("{}1{}", "{b = ".repeat(331), "}".repeat(331));
            for value in [&arrays, &tables] {
                let doc = format!("a = {value}\n");
                drop(toml::loads(&doc).expect("loads at the budget"));
            }
            let dir = std::env::temp_dir().join(format!("bm-config-deep-{}", std::process::id()));
            std::fs::create_dir_all(&dir).unwrap();
            let user = dir.join("config.toml");
            let managed = dir.join("managed.toml");
            let fs = SystemFs {
                home: Some(dir.to_string_lossy().into_owned()),
                cwd: Some(dir.to_string_lossy().into_owned()),
            };
            let env = Env::new();
            let locations = Locations {
                user_config: user.to_string_lossy().into_owned(),
                managed: ManagedSource::File(managed.to_string_lossy().into_owned()),
                default_keys_dir: dir.join("keys").to_string_lossy().into_owned(),
                default_state_dir: dir.join("state").to_string_lossy().into_owned(),
                system_dirs: Vec::new(),
                flavour: if cfg!(windows) {
                    config::Flavour::Windows
                } else {
                    config::Flavour::Posix
                },
            };
            let world = World {
                fs: &fs,
                env: &env,
                locations: &locations,
            };
            for value in [&arrays, &tables] {
                for line in [
                    format!("a = {value}\n"),
                    format!("[telemetry]\nenabled = {value}\n"),
                    format!("[storage]\ndirectory = {value}\n"),
                    format!("[scopes]\nallowed = {value}\n"),
                ] {
                    for (write, clear) in [(&user, &managed), (&managed, &user)] {
                        std::fs::write(write, &line).unwrap();
                        let _ = std::fs::remove_file(clear);
                        let _ = report::check(&world, &mut Guards::new());
                        let _ = report::show(&world, &mut Guards::new());
                        let _ = report::path(&world, &mut Guards::new());
                        let _ = report::audit_settings(&world, &mut Guards::new());
                    }
                }
            }
            std::fs::remove_dir_all(&dir).unwrap();
        })
        .unwrap();
    worker.join().expect("no overflow on a 2 MiB stack");
}
