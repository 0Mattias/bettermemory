//! The readers of one config file on hostile file systems: a 50 MB file
//! refused having read at most the bound plus one byte, a file system that
//! errors on every call, one that hands back more than asked, paths with
//! NUL and non-UTF-8 bytes, and the Windows flavour's rules (directories
//! refused at the open, a trailing separator), each a typed outcome.

use std::cell::Cell;

use bettermemory_core::fsdata::{FileId, Flavour, Fs, Kind, MemFs, Node, PyErr, Stat};
use bettermemory_core::gitconfig::file::{
    config_bytes, read_git_config, ConfigBytes, Dies, Unread,
};
use bettermemory_core::gitconfig::FORMAT_LIMIT;

/// A file of `size` bytes that counts every byte handed out.
struct Counting {
    size: usize,
    stat_size: u64,
    handed: Cell<usize>,
}

impl Fs for Counting {
    type File = usize;
    fn open(&self, _: &[u8]) -> Result<usize, PyErr> {
        Ok(0)
    }
    fn fstat(&self, _: &mut usize) -> Result<Stat, PyErr> {
        Ok(Stat {
            kind: Kind::Regular,
            identity: Some(FileId(vec![1])),
            size: self.stat_size,
        })
    }
    fn read(&self, at: &mut usize, max: usize) -> Result<Vec<u8>, PyErr> {
        // At most 64 KiB a call, as a pipe or a slow disk answers.
        let n = max.min(1 << 16).min(self.size - *at);
        *at += n;
        self.handed.set(self.handed.get() + n);
        Ok(vec![b'#'; n])
    }
    fn access_read(&self, _: &[u8]) -> bool {
        true
    }
    fn lstat(&self, _: &[u8]) -> Result<Kind, PyErr> {
        Ok(Kind::Regular)
    }
}

#[test]
fn a_50_mb_file_is_refused_having_read_at_most_the_bound_plus_one() {
    let size = 50 << 20;
    // The stat lies small, so the reader goes on to the bytes.
    let fs = Counting {
        size,
        stat_size: 1,
        handed: Cell::new(0),
    };
    assert_eq!(
        config_bytes(&fs, b"c", false),
        ConfigBytes::Overflow {
            path: b"c".to_vec()
        }
    );
    assert!(fs.handed.get() <= FORMAT_LIMIT + 1);
    fs.handed.set(0);
    let read = read_git_config(&fs, b"c", None);
    assert_eq!(read.why_text().unwrap(), b"c is larger than 2 MiB");
    assert!(fs.handed.get() <= FORMAT_LIMIT + 1);
    // A truthful stat refuses before any byte is read.
    let fs = Counting {
        size,
        stat_size: size as u64,
        handed: Cell::new(0),
    };
    assert_eq!(
        read_git_config(&fs, b"c", None).why.unwrap().1,
        Unread::TooLarge
    );
    assert_eq!(fs.handed.get(), 0);
    // Exactly the bound is read whole.
    let fs = Counting {
        size: FORMAT_LIMIT,
        stat_size: 0,
        handed: Cell::new(0),
    };
    assert!(matches!(
        config_bytes(&fs, b"c", false),
        ConfigBytes::Read { .. }
    ));
}

/// Hands back more than asked: the reader cuts to what it asked.
struct Generous;

impl Fs for Generous {
    type File = ();
    fn open(&self, _: &[u8]) -> Result<(), PyErr> {
        Ok(())
    }
    fn fstat(&self, _: &mut ()) -> Result<Stat, PyErr> {
        Ok(Stat {
            kind: Kind::Regular,
            identity: None,
            size: 0,
        })
    }
    fn read(&self, _: &mut (), max: usize) -> Result<Vec<u8>, PyErr> {
        Ok(vec![b'#'; max + 10])
    }
    fn access_read(&self, _: &[u8]) -> bool {
        true
    }
    fn lstat(&self, _: &[u8]) -> Result<Kind, PyErr> {
        Ok(Kind::Regular)
    }
}

#[test]
fn a_file_system_that_gives_too_much_is_bounded() {
    assert!(matches!(
        config_bytes(&Generous, b"c", false),
        ConfigBytes::Overflow { .. }
    ));
}

/// Errors on every call, with the error given.
struct Failing(PyErr, bool);

impl Fs for Failing {
    type File = ();
    fn open(&self, _: &[u8]) -> Result<(), PyErr> {
        if self.1 {
            Ok(())
        } else {
            Err(self.0)
        }
    }
    fn fstat(&self, _: &mut ()) -> Result<Stat, PyErr> {
        Err(self.0)
    }
    fn read(&self, _: &mut (), _: usize) -> Result<Vec<u8>, PyErr> {
        Err(self.0)
    }
    fn access_read(&self, _: &[u8]) -> bool {
        false
    }
    fn lstat(&self, _: &[u8]) -> Result<Kind, PyErr> {
        Err(self.0)
    }
}

#[test]
fn a_file_system_that_errors_everywhere_gives_typed_outcomes() {
    for error in [
        PyErr::FileNotFound,
        PyErr::NotADirectory,
        PyErr::Permission,
        PyErr::Os,
        PyErr::Value,
    ] {
        for opens in [false, true] {
            let fs = Failing(error, opens);
            let bytes = config_bytes(&fs, b"p", false);
            let read = read_git_config(&fs, b"p", None);
            if !opens && error.missing() {
                assert_eq!(bytes, ConfigBytes::Skipped);
                assert_eq!(read.why, None);
                continue;
            }
            assert_eq!(
                bytes,
                ConfigBytes::Dies(Dies::CannotRead {
                    path: b"p".to_vec(),
                    error
                })
            );
            let text = format!("git cannot read p ({})", error.name());
            if let ConfigBytes::Dies(d) = &bytes {
                assert_eq!(d.why(), text.as_bytes());
            }
            assert_eq!(
                read.why_text().unwrap(),
                format!("p could not be read ({})", error.name()).as_bytes()
            );
            if error == PyErr::Permission && !opens {
                assert_eq!(config_bytes(&fs, b"p", true), ConfigBytes::Skipped);
            }
        }
    }
}

#[test]
fn hostile_paths_are_typed_outcomes() {
    for flavour in [Flavour::Posix, Flavour::Windows] {
        let mut fs = MemFs::new(flavour);
        fs.insert(b"r/\xff", Node::File(b"[a]\nb\n".to_vec()));
        fs.insert(b"r/c", Node::File(b"[a]\nb\n".to_vec()));
        let nul = b"r/c\0x";
        assert_eq!(fs.open(nul).err(), Some(PyErr::Value));
        assert!(!fs.access_read(nul));
        assert_eq!(
            read_git_config(&fs, nul, None).why.unwrap().1,
            Unread::CouldNotRead(PyErr::Value)
        );
        let odd = read_git_config(&fs, b"r/\xff", None);
        match flavour {
            Flavour::Posix => assert_eq!(odd.entries.len(), 1),
            // The reference's paths are text, a path here its UTF-8.
            Flavour::Windows => assert_eq!(odd.why.unwrap().1, Unread::CouldNotRead(PyErr::Value)),
        }
        // A long path of `..` and links that loop is a bounded walk.
        let mut long = b"r".to_vec();
        for _ in 0..10_000 {
            long.extend_from_slice(b"/c/..");
        }
        assert!(matches!(
            fs.open(&long),
            Err(PyErr::NotADirectory | PyErr::FileNotFound)
        ));
        fs.insert(b"r/l", Node::Link(b"m".to_vec()));
        fs.insert(b"r/m", Node::Link(b"l".to_vec()));
        assert_eq!(fs.open(b"r/l").err(), Some(PyErr::Os));
    }
}

#[test]
fn the_windows_flavour_refuses_a_directory_at_the_open() {
    let mut fs = MemFs::new(Flavour::Windows);
    fs.insert(b"r/d/x", Node::File(Vec::new()));
    fs.insert(b"r/c", Node::File(b"[a]\nb\n".to_vec()));
    // `_wopen` of a directory: EACCES.
    assert_eq!(
        config_bytes(&fs, b"r/d", false),
        ConfigBytes::Dies(Dies::CannotRead {
            path: b"r/d".to_vec(),
            error: PyErr::Permission
        })
    );
    assert_eq!(config_bytes(&fs, b"r/d", true), ConfigBytes::Skipped);
    assert_eq!(
        read_git_config(&fs, b"r/d", None).why_text().unwrap(),
        b"r/d could not be read (PermissionError)"
    );
    // A trailing separator: the open finds nothing, `_waccess` finds the
    // directory, and git dies (the empty `include.path` of `_accessible`).
    for path in [&b"r/d/"[..], b"r\\d\\"] {
        assert_eq!(fs.open(path).err(), Some(PyErr::FileNotFound));
        assert!(fs.access_read(path));
        assert!(matches!(
            config_bytes(&fs, path, false),
            ConfigBytes::Dies(_)
        ));
        assert_eq!(read_git_config(&fs, path, None).why, None);
    }
    assert_eq!(config_bytes(&fs, b"r/c/", false), ConfigBytes::Skipped);
    // A file where a directory of the path should be: not found.
    assert_eq!(fs.open(b"r/c/x").err(), Some(PyErr::FileNotFound));
    assert_eq!(fs.open(b"r\\c").map(|_| ()), Ok(()));
}

#[test]
fn a_dangling_link_is_found_by_access_on_windows_only() {
    for flavour in [Flavour::Posix, Flavour::Windows] {
        let mut fs = MemFs::new(flavour);
        fs.insert(b"r/l", Node::Link(b"missing".to_vec()));
        let got = config_bytes(&fs, b"r/l", false);
        match flavour {
            Flavour::Posix => {
                assert!(!fs.access_read(b"r/l"));
                assert_eq!(got, ConfigBytes::Skipped);
            }
            // `GetFileAttributesW` describes the link itself.
            Flavour::Windows => {
                assert!(fs.access_read(b"r/l"));
                assert_eq!(
                    got,
                    ConfigBytes::Dies(Dies::CannotRead {
                        path: b"r/l".to_vec(),
                        error: PyErr::FileNotFound
                    })
                );
            }
        }
    }
}

#[test]
fn links_are_followed_to_the_hop_limit() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"r/c", Node::File(b"[a]\nb\n".to_vec()));
    let mut prev = b"c".to_vec();
    for i in 0..33 {
        let name = format!("l{i}").into_bytes();
        let mut path = b"r/".to_vec();
        path.extend_from_slice(&name);
        fs.insert(&path, Node::Link(prev.clone()));
        prev = name;
    }
    assert_eq!(fs.link_hops, bettermemory_core::fsdata::LINK_HOPS);
    assert!(fs.open(b"r/l31").is_ok());
    assert_eq!(fs.open(b"r/l32").err(), Some(PyErr::Os));
    fs.link_hops = 40;
    assert!(fs.open(b"r/l32").is_ok());
}

#[test]
fn modes_hard_links_and_the_zero_device() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"r/d/c", Node::File(b"[a]\nb\n".to_vec()));
    fs.insert(b"r/h", Node::Hard(b"r/d/c".to_vec()));
    fs.insert(b"z", Node::Endless);
    assert!(matches!(
        config_bytes(&fs, b"z", false),
        ConfigBytes::Overflow { .. }
    ));
    assert_eq!(
        read_git_config(&fs, b"z", None).why.unwrap().1,
        Unread::NotRegular
    );
    let id = |p: &[u8]| match config_bytes(&fs, p, false) {
        ConfigBytes::Read { identity, .. } => identity,
        other => panic!("{other:?}"),
    };
    assert_eq!(id(b"r/h"), id(b"r/d/c"));
    fs.set_mode(b"r/d", 0o600);
    assert_eq!(fs.open(b"r/d/c").err(), Some(PyErr::Permission));
    assert!(fs.open(b"r/h").is_ok());
    fs.set_mode(b"r/d/c", 0o200);
    assert_eq!(fs.open(b"r/h").err(), Some(PyErr::Permission));
}

#[test]
fn hops_are_counted_across_the_whole_path() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"r/d/c", Node::File(b"[a]\nb\n".to_vec()));
    fs.insert(b"r/l", Node::Link(b"d".to_vec()));
    fs.insert(b"r/y", Node::Link(b"d".to_vec()));
    fs.insert(b"r/x", Node::Link(b"y".to_vec()));
    let path = |piece: &str, n: usize| format!("r/{}d/c", piece.repeat(n)).into_bytes();
    // One hop a component: 32 pass, the 33rd fails.
    assert!(fs.open(&path("l/../", 32)).is_ok());
    assert_eq!(fs.open(&path("l/../", 33)).err(), Some(PyErr::Os));
    // Two hops a component, as measured on macOS: 16 repeats (32 hops)
    // pass, 17 (34) fail.
    assert!(fs.open(&path("x/../", 16)).is_ok());
    assert_eq!(fs.open(&path("x/../", 17)).err(), Some(PyErr::Os));
}

#[test]
fn an_empty_path_or_link_target_names_nothing() {
    for flavour in [Flavour::Posix, Flavour::Windows] {
        let mut fs = MemFs::new(flavour);
        fs.insert(b"h/x", Node::File(Vec::new()));
        fs.insert(b"h/e", Node::Link(Vec::new()));
        assert_eq!(fs.open(b"").err(), Some(PyErr::FileNotFound));
        assert!(!fs.access_read(b""));
        assert_eq!(config_bytes(&fs, b"", false), ConfigBytes::Skipped);
        assert_eq!(read_git_config(&fs, b"", None).why, None);
        // macOS answers ENOENT for a link to ""; Linux refuses to make
        // one (symlink(2) with an empty target is ENOENT), so the POSIX
        // fixture holds no such case.
        assert_eq!(fs.open(b"h/e").err(), Some(PyErr::FileNotFound));
        assert_eq!(fs.open(b"h/e/c").err(), Some(PyErr::FileNotFound));
    }
}
