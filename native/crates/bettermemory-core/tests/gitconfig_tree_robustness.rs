//! The tree reader on hostile trees: include cycles, 10,000 includes
//! stopped at the 64-file bound having opened no more, chains past the
//! depth, a 50 MB include refused at the bound, a file system that errors
//! on every call, values with NUL and non-UTF-8 bytes, and hard links
//! under the Windows flavour, whose identities differ.

use std::cell::Cell;

use bettermemory_core::fsdata::{Flavour, Fs, Kind, MemFs, Node, PyErr, Stat};
use bettermemory_core::gitconfig::paths::Machine;
use bettermemory_core::gitconfig::tree::{config_tree, Ask, ConfigValues};
use bettermemory_core::gitconfig::FORMAT_LIMIT;

fn none(_: &[u8]) -> Option<Vec<u8>> {
    None
}

fn read<F: Fs>(fs: &F, flavour: Flavour, path: &[u8]) -> ConfigValues {
    let machine = Machine {
        flavour,
        home: Some(b"/h".to_vec()),
        profile: None,
        user_home: &none,
    };
    config_tree(
        fs,
        &machine,
        path,
        &Ask {
            stamp_self: true,
            ..Ask::default()
        },
    )
}

fn inc(paths: &[&str]) -> Vec<u8> {
    paths
        .iter()
        .map(|p| format!("[include]\n\tpath = {p}\n"))
        .collect::<String>()
        .into_bytes()
}

/// Counts opens and bytes handed out.
struct Counting {
    inner: MemFs,
    opens: Cell<usize>,
    bytes: Cell<usize>,
}

impl Fs for Counting {
    type File = <MemFs as Fs>::File;
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.opens.set(self.opens.get() + 1);
        self.inner.open(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        let got = self.inner.read(file, max)?;
        self.bytes.set(self.bytes.get() + got.len());
        Ok(got)
    }
    fn access_read(&self, path: &[u8]) -> bool {
        self.inner.access_read(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.readlink(path)
    }
}

fn counting(inner: MemFs) -> Counting {
    Counting {
        inner,
        opens: Cell::new(0),
        bytes: Cell::new(0),
    }
}

#[test]
fn cycles_through_one_file_and_several_die() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/r/c", Node::File(inc(&["c"])));
    let got = read(&fs, Flavour::Posix, b"/r/c");
    assert_eq!(
        got.dies.unwrap(),
        b"/r/c is included in itself, which git follows past its depth"
    );
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/r/a", Node::File(inc(&["b"])));
    fs.insert(b"/r/b", Node::File(inc(&["c"])));
    fs.insert(b"/r/c", Node::File(inc(&["a"])));
    let got = read(&fs, Flavour::Posix, b"/r/a");
    assert_eq!(
        got.dies.unwrap(),
        b"/r/a is included in itself, which git follows past its depth"
    );
    assert_eq!(got.files.len(), 4);
}

#[test]
fn ten_thousand_includes_stop_at_the_file_bound() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/r/c", Node::File(inc(&["x"; 10_000])));
    fs.insert(b"/r/x", Node::File(b"[a]\n\tb = 1\n".to_vec()));
    let fs = counting(fs);
    let got = read(&fs, Flavour::Posix, b"/r/c");
    assert!(!got.complete);
    assert!(got.dies.is_none());
    assert_eq!(got.files.len(), 64);
    // The 65th file is counted and never opened.
    assert_eq!(fs.opens.get(), 64);
    assert_eq!(got.values[1].1.len(), 63);
}

#[test]
fn chains_past_the_depth_die_at_eleven() {
    let mut fs = MemFs::new(Flavour::Posix);
    for i in 0..30 {
        let next = format!("f{}", i + 1);
        fs.insert(format!("/r/f{i}").as_bytes(), Node::File(inc(&[&next])));
    }
    fs.insert(b"/r/f30", Node::File(Vec::new()));
    let got = read(&fs, Flavour::Posix, b"/r/f0");
    assert_eq!(
        got.dies.unwrap(),
        b"/r/f11 is included past git's depth of 10"
    );
    // A chain into nothing ends at the missing file, whatever its depth.
    let mut fs = MemFs::new(Flavour::Posix);
    for i in 0..11 {
        let next = format!("f{}", i + 1);
        fs.insert(format!("/r/f{i}").as_bytes(), Node::File(inc(&[&next])));
    }
    let got = read(&fs, Flavour::Posix, b"/r/f0");
    assert_eq!(got.dies, None);
    assert!(got.complete);
}

#[test]
fn a_50_mb_include_is_refused_at_the_bound() {
    let mut fs = MemFs::new(Flavour::Posix);
    let top = inc(&["big"]);
    fs.insert(b"/r/c", Node::File(top.clone()));
    fs.insert(b"/r/big", Node::File(vec![b'#'; 50 << 20]));
    let fs = counting(fs);
    let got = read(&fs, Flavour::Posix, b"/r/c");
    assert!(!got.complete);
    assert!(fs.bytes.get() <= top.len() + FORMAT_LIMIT + 1);
}

struct Failing(PyErr);

impl Fs for Failing {
    type File = ();
    fn open(&self, _: &[u8]) -> Result<(), PyErr> {
        Err(self.0)
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
    fn readlink(&self, _: &[u8]) -> Result<Vec<u8>, PyErr> {
        Err(self.0)
    }
}

#[test]
fn a_file_system_that_errors_everywhere_gives_typed_outcomes() {
    for error in [
        PyErr::FileNotFound,
        PyErr::Permission,
        PyErr::Os,
        PyErr::Value,
    ] {
        let got = read(&Failing(error), Flavour::Posix, b"/c");
        if error.missing() {
            assert_eq!((got.dies, got.complete), (None, true));
        } else {
            let want = format!("git cannot read /c ({})", error.name());
            assert_eq!(got.dies.unwrap(), want.as_bytes());
        }
    }
}

#[test]
fn hostile_values_are_typed_outcomes() {
    for flavour in [Flavour::Posix, Flavour::Windows] {
        let mut fs = MemFs::new(flavour);
        // A NUL cuts the value; the rest is a name like any other.
        fs.insert(
            b"/r/c",
            Node::File(b"[include]\n\tpath = x\x00/etc\n".to_vec()),
        );
        fs.insert(b"/r/x", Node::File(b"[a]\n\tb\n".to_vec()));
        let got = read(&fs, flavour, b"/r/c");
        assert_eq!(got.dies, None);
        assert_eq!(got.values.len(), 2);
        // A value that is not UTF-8.
        let mut fs = MemFs::new(flavour);
        fs.insert(
            b"/r/c",
            Node::File(b"[include]\n\tpath = \xff\xfe\n".to_vec()),
        );
        let got = read(&fs, flavour, b"/r/c");
        match flavour {
            Flavour::Posix => assert_eq!((got.dies, got.complete), (None, true)),
            // Windows decodes a path as UTF-8 (`ValueError` here; the
            // reference's `os.fsdecode` raises there, by reading).
            Flavour::Windows => assert!(got.dies.unwrap().ends_with(b"(ValueError)")),
        }
        let mut fs = MemFs::new(flavour);
        fs.insert(
            b"/r/c",
            Node::File(b"[include]\n\tpath = ~\xff/x\n".to_vec()),
        );
        let got = read(&fs, flavour, b"/r/c");
        assert!(got
            .dies
            .unwrap()
            .ends_with(b"includes '~\\udcff/x', which git cannot expand"));
    }
}

#[test]
fn a_cycle_through_hard_links_is_caught_by_identity_or_by_the_depth() {
    for flavour in [Flavour::Posix, Flavour::Windows] {
        let mut fs = MemFs::new(flavour);
        // One file under a dozen names, each naming the next: `d/c`
        // taken from each name's own directory.
        fs.insert(b"/r/c", Node::File(inc(&["d/c"])));
        let mut name = b"/r".to_vec();
        for _ in 0..12 {
            name.extend_from_slice(b"/d");
            let mut file = name.clone();
            file.extend_from_slice(b"/c");
            fs.insert(&file, Node::Hard(b"/r/c".to_vec()));
        }
        let got = read(&fs, flavour, b"/r/c");
        let dies = got.dies.unwrap();
        match flavour {
            Flavour::Posix => {
                assert!(dies.ends_with(b"is included in itself, which git follows past its depth"))
            }
            // Each name its own identity: only the depth stops it (the
            // reference there takes the file index and says "in itself").
            Flavour::Windows => assert!(dies.ends_with(b"is included past git's depth of 10")),
        }
    }
}

#[test]
fn an_unknown_include_if_condition_reads_nothing_and_is_not_declined() {
    for sub in [
        "foo:bar",
        "",
        "Gitdir:x",
        "gitdir :x",
        "hasconfig:other:x",
        "hasconfig:remote.*.url",
    ] {
        let mut fs = MemFs::new(Flavour::Posix);
        let text = format!("[includeIf \"{sub}\"]\n\tpath = x\n\tpath\n[a]\n\tb = 1\n");
        fs.insert(b"/r/c", Node::File(text.into_bytes()));
        fs.insert(b"/r/x", Node::File(b"[x]\n\ty = 1\n".to_vec()));
        let got = read(&fs, Flavour::Posix, b"/r/c");
        assert_eq!(got.not_ported, None, "{sub:?}");
        assert_eq!(
            (got.dies, got.complete, got.files.len()),
            (None, true, 1),
            "{sub:?}"
        );
    }
    for sub in [
        "gitdir:x",
        "gitdir/i:x",
        "onbranch:x",
        "hasconfig:remote.*.url:x",
    ] {
        let mut fs = MemFs::new(Flavour::Posix);
        let text = format!("[includeIf \"{sub}\"]\n\tpath = x\n");
        fs.insert(b"/r/c", Node::File(text.into_bytes()));
        let got = read(&fs, Flavour::Posix, b"/r/c");
        assert_eq!(got.not_ported.as_deref(), Some(sub.as_bytes()));
    }
}
