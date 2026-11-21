//! Cases a plausible wrong first-run write fails: one that writes over a
//! file that is there, prints the notice when it wrote nothing, writes
//! before the parents exist, reads an `exists` error as "no file", makes
//! the parents with `create_dir_all`'s classes, or skips the read after
//! writing. The effects are `SystemCreate`'s, wrapped to log or fail.

mod common;

use std::cell::RefCell;

use bettermemory_core::config::create::{
    created_notice, load_config_creating, Create, SystemCreate, DEFAULT_CONFIG,
};
use bettermemory_core::config::report::SystemFs;
use bettermemory_core::config::{
    Ctx, Env, Flavour, Guards, Locations, ManagedSource, Raised, Setting, World,
};
use common::settings_create::scratch;

/// `SystemCreate`, with each call logged and the ones named failing.
struct Logged {
    calls: RefCell<Vec<&'static str>>,
    fail: &'static [&'static str],
}

impl Logged {
    fn new(fail: &'static [&'static str]) -> Logged {
        Logged {
            calls: RefCell::new(Vec::new()),
            fail,
        }
    }

    fn step(&self, name: &'static str) -> Result<(), Raised> {
        self.calls.borrow_mut().push(name);
        if self.fail.contains(&name) {
            return Err(Raised {
                class: "PermissionError",
                message: format!("{name} refused"),
            });
        }
        Ok(())
    }
}

impl Create for Logged {
    fn mkdir_parent(&self, path: &str) -> Result<(), Raised> {
        self.step("mkdir")?;
        SystemCreate.mkdir_parent(path)
    }
    fn exists(&self, path: &str) -> Result<bool, Raised> {
        self.step("exists")?;
        SystemCreate.exists(path)
    }
    fn write_atomic(&self, path: &str, data: &[u8]) -> Result<(), Raised> {
        self.step("write")?;
        SystemCreate.write_atomic(path, data)
    }
}

fn run(create: &dyn Create, path: &str) -> (bool, Result<Setting, Raised>) {
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
    let (made, loaded) = load_config_creating(&world, create, path, &mut ctx);
    (made, loaded.map(|c| c.get("storage", "cwd_store").clone()))
}

#[test]
fn a_file_that_is_there_is_read_never_rewritten() {
    let dir = scratch();
    let path = dir.0.join("config.toml");
    std::fs::write(&path, "[storage]\ncwd_store = false\n").unwrap();
    let logged = Logged::new(&[]);
    let (made, cwd) = run(&logged, path.to_str().unwrap());
    assert!(!made);
    assert_eq!(cwd.unwrap(), Setting::Bool(false));
    assert_eq!(*logged.calls.borrow(), ["mkdir", "exists"]);
    assert_eq!(
        std::fs::read(&path).unwrap(),
        b"[storage]\ncwd_store = false\n"
    );
}

#[test]
fn a_missing_file_is_written_whole_after_the_parents() {
    let dir = scratch();
    let path = dir.0.join("a").join("b").join("config.toml");
    let logged = Logged::new(&[]);
    let (made, cwd) = run(&logged, path.to_str().unwrap());
    assert!(made);
    assert_eq!(cwd.unwrap(), Setting::Bool(true));
    assert_eq!(*logged.calls.borrow(), ["mkdir", "exists", "write"]);
    assert_eq!(std::fs::read(&path).unwrap(), DEFAULT_CONFIG.as_bytes());
    assert!(DEFAULT_CONFIG.starts_with("# bettermemory config\n"));
}

#[test]
fn an_exists_error_stops_the_load_and_writes_nothing() {
    let dir = scratch();
    let path = dir.0.join("config.toml");
    let logged = Logged::new(&["exists"]);
    let (made, out) = run(&logged, path.to_str().unwrap());
    assert!(!made);
    assert_eq!(out.unwrap_err().class, "PermissionError");
    assert_eq!(*logged.calls.borrow(), ["mkdir", "exists"]);
    assert!(!path.exists());
}

#[test]
fn a_failed_write_makes_no_notice() {
    let dir = scratch();
    let path = dir.0.join("config.toml");
    let logged = Logged::new(&["write"]);
    let (made, out) = run(&logged, path.to_str().unwrap());
    assert!(!made);
    assert!(out.is_err());
}

#[test]
fn a_failed_mkdir_stops_before_exists() {
    let dir = scratch();
    let path = dir.0.join("config.toml");
    let logged = Logged::new(&["mkdir"]);
    let (made, out) = run(&logged, path.to_str().unwrap());
    assert!(!made);
    assert!(out.is_err());
    assert_eq!(*logged.calls.borrow(), ["mkdir"]);
}

/// A writer that reports success and writes nothing: the read after it is
/// FileNotFoundError, not the defaults `create=False` gives, and the
/// notice still stands.
struct Vanishing;

impl Create for Vanishing {
    fn mkdir_parent(&self, path: &str) -> Result<(), Raised> {
        SystemCreate.mkdir_parent(path)
    }
    fn exists(&self, _: &str) -> Result<bool, Raised> {
        Ok(false)
    }
    fn write_atomic(&self, _: &str, _: &[u8]) -> Result<(), Raised> {
        Ok(())
    }
}

#[test]
fn a_file_gone_after_the_write_is_file_not_found() {
    let dir = scratch();
    let path = dir.0.join("config.toml");
    let text = path.to_str().unwrap();
    let (made, out) = run(&Vanishing, text);
    assert!(made);
    let raised = out.unwrap_err();
    assert_eq!(raised.class, "FileNotFoundError");
    assert!(raised
        .message
        .starts_with("[Errno 2] No such file or directory: "));
}

#[cfg(unix)]
#[test]
fn a_file_parent_is_file_exists_and_a_file_grandparent_not_a_directory() {
    let dir = scratch();
    std::fs::write(dir.0.join("f"), b"").unwrap();
    let parent = dir.0.join("f").join("config.toml");
    let (_, out) = run(&SystemCreate, parent.to_str().unwrap());
    assert_eq!(out.unwrap_err().class, "FileExistsError");
    let deeper = dir.0.join("f").join("a").join("config.toml");
    let (_, out) = run(&SystemCreate, deeper.to_str().unwrap());
    assert_eq!(out.unwrap_err().class, "NotADirectoryError");
}

#[test]
fn the_notice_names_the_path_as_given() {
    assert_eq!(
        created_notice("a/config.toml"),
        "[bettermemory] created default config at a/config.toml\n"
    );
}

/// `Path.mkdir(parents=False, exist_ok=True)` under a missing parent raises
/// FileNotFoundError and makes nothing; a mkdir that ignores `parents`
/// makes the parent.
#[test]
fn mkdir_without_parents_raises_under_a_missing_parent() {
    let dir = scratch();
    let target = dir.0.join("missing").join("leaf");
    let err = bettermemory_core::config::create::mkdir(&target, false).unwrap_err();
    assert_eq!(err.class, "FileNotFoundError");
    assert!(!dir.0.join("missing").exists());
    bettermemory_core::config::create::mkdir(&target, true).unwrap();
    assert!(target.is_dir());
}

/// CPython strips the trailing period (and spaces and line breaks) of a
/// Windows message; std's text keeps the period.
#[test]
fn the_windows_message_loses_its_trailing_period() {
    use bettermemory_core::config::create::winerror_text;
    assert_eq!(
        winerror_text("Cannot create a file when that file already exists."),
        "Cannot create a file when that file already exists"
    );
    assert_eq!(winerror_text("Access is denied. \r\n"), "Access is denied");
    assert_eq!(winerror_text("v1.2 ok"), "v1.2 ok");
    assert_eq!(winerror_text("..."), "");
}
