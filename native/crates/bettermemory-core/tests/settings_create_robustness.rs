//! Hostile inputs to the first-run write: a NUL in the path, a name past
//! the file system's limit, the root, a deep missing parent chain, and a
//! large file that is there. No panic, in debug or release; the chain and
//! the read grow linearly (by the wall clock: the timed part waits on the
//! disk).

mod common;
mod timing;

use bettermemory_core::config::create::{
    ignored_code, is_posix_spelled, load_config_creating, Create, SystemCreate,
};
use bettermemory_core::config::report::SystemFs;
use bettermemory_core::config::{
    Ctx, Env, Flavour, Guards, Locations, ManagedSource, Raised, World,
};
use common::settings_create::scratch;

fn run(path: &str) -> (bool, Result<(), Raised>) {
    let fs = SystemFs {
        home: None,
        cwd: None,
    };
    let env = Env::new();
    let locations = Locations {
        user_config: path.to_owned(),
        managed: ManagedSource::File(format!("{path}.no-managed")),
        default_keys_dir: String::new(),
        default_state_dir: String::new(),
        system_dirs: Vec::new(),
        flavour: if cfg!(windows) {
            Flavour::Windows
        } else {
            Flavour::Posix
        },
    };
    let world = World {
        fs: &fs,
        env: &env,
        locations: &locations,
    };
    let mut guards = Guards::new();
    let mut ctx = Ctx::new(&mut guards);
    let (made, out) = load_config_creating(&world, &SystemCreate, path, &mut ctx);
    (made, out.map(|_| ()))
}

#[test]
fn a_nul_in_the_path_raises_value_error() {
    let dir = scratch();
    let path = format!("{}/a\u{0}b/config.toml", dir.0.display());
    let (made, out) = run(&path);
    assert!(!made);
    assert_eq!(out.unwrap_err().class, "ValueError");
}

#[test]
fn a_name_past_the_limit_raises() {
    let dir = scratch();
    let path = dir.0.join("n".repeat(100_000)).join("config.toml");
    let (made, out) = run(path.to_str().unwrap());
    assert!(!made);
    assert!(out.is_err());
}

#[cfg(unix)]
#[test]
fn the_root_is_a_directory() {
    let (made, out) = run("/");
    assert!(!made);
    assert_eq!(out.unwrap_err().class, "IsADirectoryError");
}

fn chain(depth: usize) -> std::time::Duration {
    let dir = scratch();
    let mut path = dir.0.clone();
    for _ in 0..depth {
        path = path.join("d");
    }
    let path = path.join("config.toml");
    let text = path.to_str().unwrap().to_owned();
    timing::wall(|| {
        let (made, out) = run(&text);
        assert!(made && out.is_ok());
    })
}

#[test]
fn a_deep_missing_chain_is_linear() {
    timing::grows_within(
        "a missing chain of 10 and 100 directories",
        timing::TENFOLD,
        &mut || chain(10),
        &mut || chain(100),
    );
}

fn large(lines: usize) -> std::time::Duration {
    let dir = scratch();
    let path = dir.0.join("config.toml");
    let mut text = String::from("[storage]\ncwd_store = false\n");
    for i in 0..lines {
        text.push_str(&format!("# comment line {i}\n"));
    }
    std::fs::write(&path, text).unwrap();
    let path = path.to_str().unwrap().to_owned();
    timing::wall(|| {
        let (made, out) = run(&path);
        assert!(!made && out.is_ok());
    })
}

#[test]
fn a_large_file_there_is_read_linearly() {
    timing::grows_within(
        "a config of 10,000 and 100,000 lines",
        timing::TENFOLD,
        &mut || large(10_000),
        &mut || large(100_000),
    );
}

#[test]
fn a_nul_in_the_name_makes_the_parents_then_raises_from_open() {
    let dir = scratch();
    let path = format!("{}/d/config.toml\u{0}", dir.0.display());
    let (made, out) = run(&path);
    assert!(!made);
    let raised = out.unwrap_err();
    assert_eq!(raised.class, "ValueError");
    assert_eq!(raised.message, "open: embedded null character in path");
    assert!(dir.0.join("d").is_dir());
}

#[test]
fn a_bare_name_makes_no_parent() {
    // `Path("config.toml").parent` is `.`, which is there.
    assert_eq!(SystemCreate.mkdir_parent("config.toml"), Ok(()));
}

#[test]
fn exists_under_a_file_is_false() {
    // ENOTDIR (Windows: ERROR_PATH_NOT_FOUND) is no file to Path.exists.
    let dir = scratch();
    std::fs::write(dir.0.join("f"), b"").unwrap();
    let path = dir.0.join("f").join("x");
    assert_eq!(SystemCreate.exists(path.to_str().unwrap()), Ok(false));
}

#[test]
fn exists_raises_on_einval_and_the_windows_directory_error() {
    assert!(!ignored_code(22));
    #[cfg(unix)]
    assert!(ignored_code(20));
    #[cfg(windows)]
    assert!(!ignored_code(267));
}

#[cfg(unix)]
#[test]
fn a_write_over_a_directory_is_is_a_directory_error() {
    // os.replace(tmp, dir) raises IsADirectoryError on macOS and Linux.
    let dir = scratch();
    std::fs::create_dir(dir.0.join("c")).unwrap();
    let path = dir.0.join("c");
    let raised = SystemCreate
        .write_atomic(path.to_str().unwrap(), b"x")
        .unwrap_err();
    assert_eq!(raised.class, "IsADirectoryError");
}

#[test]
fn the_posix_spelling_is_the_one_path_prints() {
    for good in ["config.toml", "a/b", "/a/b", "//a", "/", ".", "//"] {
        assert!(is_posix_spelled(good), "{good:?}");
    }
    for bad in ["", "config.toml/", "./x", "a//b", "a/./b", "///a", "a/."] {
        assert!(!is_posix_spelled(bad), "{bad:?}");
    }
}
