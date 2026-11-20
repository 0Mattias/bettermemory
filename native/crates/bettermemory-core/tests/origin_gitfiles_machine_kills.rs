//! Cases a plausible wrong port of `_read_prefix`, `relative_to_root` and
//! `_spelled_as_git` fails, each named by the mistake it catches.

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFile, MemFs, Node, OsError, PyErr, Refused, Stat, StatResult,
};
use bettermemory_core::origin_gitfiles::machine::{
    read_prefix, relative_to_root, spelled_as_git, NotRelative,
};

/// `MemFs` whose reads give at most `cap` bytes each, and which may
/// decline the calls only some file systems offer.
struct Short {
    inner: MemFs,
    cap: usize,
    offers: bool,
}

impl Fs for Short {
    type File = MemFile;
    fn open(&self, path: &[u8]) -> Result<MemFile, PyErr> {
        self.inner.open(path)
    }
    fn fstat(&self, file: &mut MemFile) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn read(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, PyErr> {
        self.inner.read(file, max.min(self.cap))
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.inner.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.readlink(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.listdir(path)
    }
    fn rewind(&self, file: &mut MemFile) -> Result<(), Refused> {
        if self.offers {
            self.inner.rewind(file)
        } else {
            Err(Refused::NotOffered)
        }
    }
    fn kernel_path(&self, file: &mut MemFile) -> Result<Vec<u8>, Refused> {
        if self.offers {
            self.inner.kernel_path(file)
        } else {
            Err(Refused::NotOffered)
        }
    }
}

fn tree() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w/f", Node::File(b"0123456789".to_vec()));
    fs.insert(b"/w/d/e/x", Node::File(b"x".to_vec()));
    fs.insert(b"/w/ld", Node::Link(b"/w/d".to_vec()));
    fs.insert(b"/w/fifo", Node::Fifo);
    fs.insert(b"/w/zero", Node::Endless);
    fs
}

fn short(cap: usize) -> Short {
    Short {
        inner: tree(),
        cap,
        offers: true,
    }
}

fn value_error(m: &str) -> Result<Vec<u8>, NotRelative> {
    Err(NotRelative::ValueError(m.as_bytes().to_vec()))
}

#[test]
fn a_short_read_is_followed_by_another() {
    let fs = short(3);
    let mut f = fs.open(b"/w/f").unwrap();
    assert_eq!(read_prefix(&fs, &mut f, 8).unwrap(), b"01234567");
    assert_eq!(read_prefix(&fs, &mut f, 100).unwrap(), b"0123456789");
}

#[test]
fn the_read_starts_at_the_first_byte_whatever_was_read_before() {
    let fs = tree();
    let mut f = fs.open(b"/w/f").unwrap();
    fs.read(&mut f, 7).unwrap();
    assert_eq!(read_prefix(&fs, &mut f, 4).unwrap(), b"0123");
}

#[test]
fn a_device_without_end_gives_exactly_the_length() {
    let fs = short(5);
    let mut f = fs.open(b"/w/zero").unwrap();
    assert_eq!(read_prefix(&fs, &mut f, 12).unwrap(), vec![0; 12]);
}

#[test]
fn a_zero_length_reads_nothing_even_from_a_folder() {
    let fs = tree();
    let mut d = fs.open(b"/w/d").unwrap();
    assert_eq!(read_prefix(&fs, &mut d, 0).unwrap(), b"");
    assert_eq!(
        read_prefix(&fs, &mut d, 1),
        Err(Refused::Raised(PyErr::IsADirectory))
    );
}

#[test]
fn a_fifo_raises_at_the_seek_before_any_read() {
    let fs = tree();
    let mut p = fs.open(b"/w/fifo").unwrap();
    assert_eq!(read_prefix(&fs, &mut p, 0), Err(Refused::Raised(PyErr::Os)));
}

#[test]
fn a_file_system_without_a_seek_declines() {
    let fs = Short {
        inner: tree(),
        cap: 10,
        offers: false,
    };
    let mut f = fs.open(b"/w/f").unwrap();
    assert_eq!(read_prefix(&fs, &mut f, 1), Err(Refused::NotOffered));
}

#[test]
fn the_spelling_is_compared_component_by_component_not_as_bytes() {
    let fs = tree();
    // A byte prefix that is no component prefix: `/w/d` is not `/w/de`'s.
    assert_eq!(
        relative_to_root(&fs, b"/w/de/x", b"/w/d"),
        value_error("/w/de/x is not in /w/d")
    );
    assert_eq!(
        relative_to_root(&fs, b"/w//d/./e/", b"/w/d/"),
        Ok(b"e".to_vec())
    );
    // `..` is a component like any other, never folded.
    assert_eq!(
        relative_to_root(&fs, b"/w/d/../d/x", b"/w/d/.."),
        Ok(b"d/x".to_vec())
    );
}

#[test]
fn two_leading_slashes_are_another_root_three_are_one() {
    let fs = tree();
    assert_eq!(
        relative_to_root(&fs, b"//w/d/x", b"//w"),
        Ok(b"d/x".to_vec())
    );
    assert_eq!(
        relative_to_root(&fs, b"///w/d/x", b"/w"),
        Ok(b"d/x".to_vec())
    );
    // `//w` and `/w` are one folder on disk: the parent anchors it.
    assert_eq!(
        relative_to_root(&fs, b"//w/d/x", b"/w/d"),
        Ok(b"x".to_vec())
    );
}

#[test]
fn two_leading_slashes_on_one_side_alone_are_spelled_as_given() {
    let fs = tree();
    assert_eq!(
        relative_to_root(&fs, b"//q/x", b"/w/d"),
        value_error("//q/x is not in /w/d")
    );
    assert_eq!(
        relative_to_root(&fs, b"/w/d/x", b"//w/missing"),
        value_error("//w/missing cannot be read")
    );
}

#[test]
fn an_absolute_candidate_is_not_in_a_relative_root_of_the_same_components() {
    let fs = tree();
    assert_eq!(
        relative_to_root(&fs, b"/a/b", b"a"),
        value_error("a cannot be read")
    );
}

#[test]
fn a_relative_candidate_is_anchored_by_dot() {
    // `MemFs` reads a relative path from `/`: `Path(".").stat()` is `/`.
    let fs = tree();
    assert_eq!(relative_to_root(&fs, b"x/y", b"/"), Ok(b"x/y".to_vec()));
}

#[test]
fn the_same_path_is_dot_and_an_empty_path_is_dot() {
    let fs = tree();
    assert_eq!(relative_to_root(&fs, b"/w/d", b"/w/d/"), Ok(b".".to_vec()));
    assert_eq!(relative_to_root(&fs, b"", b"."), Ok(b".".to_vec()));
    assert_eq!(relative_to_root(&fs, b"a/b", b""), Ok(b"a/b".to_vec()));
}

#[test]
fn the_nearest_parent_on_disk_anchors_and_the_candidate_itself_does_not() {
    let fs = tree();
    assert_eq!(
        relative_to_root(&fs, b"/w/ld/e/x", b"/w/d"),
        Ok(b"e/x".to_vec())
    );
    assert_eq!(
        relative_to_root(&fs, b"/w/ld/e/x", b"/w/d/e"),
        Ok(b"x".to_vec())
    );
    // The candidate is the root's folder, but only its parents are asked.
    assert_eq!(
        relative_to_root(&fs, b"/w/ld", b"/w/d"),
        value_error("/w/ld is not in /w/d")
    );
}

#[test]
fn the_root_is_stated_once_spelled_as_a_path() {
    let fs = tree();
    assert_eq!(
        relative_to_root(&fs, b"/w/d/x", b"/w/missing//"),
        value_error("/w/missing cannot be read")
    );
    // A trailing separator after a file is dropped before the stat.
    assert_eq!(
        relative_to_root(&fs, b"/w/d/x", b"/w/f/"),
        value_error("/w/d/x is not in /w/f")
    );
}

#[test]
fn a_nul_raises_stat_s_value_error_and_is_not_passed_over() {
    let fs = tree();
    let nul = "stat: embedded null character in path";
    assert_eq!(
        relative_to_root(&fs, b"/w/d/x", b"/w/a\0b"),
        value_error(nul)
    );
    assert_eq!(
        relative_to_root(&fs, b"/w/a\0b/c", b"/w/d"),
        value_error(nul)
    );
    // The NUL is only in the candidate's last component, never stated.
    assert_eq!(
        relative_to_root(&fs, b"/w/ld/a\0b", b"/w/d"),
        Ok(b"a\0b".to_vec())
    );
    // By spelling, nothing is stated at all.
    assert_eq!(
        relative_to_root(&fs, b"/w/a\0b/c", b"/w/a\0b"),
        Ok(b"c".to_vec())
    );
}

#[test]
fn off_macos_the_root_is_given_back_spelled_as_a_path_and_nothing_is_read() {
    let fs = Short {
        inner: tree(),
        cap: 1,
        offers: false,
    };
    assert_eq!(
        spelled_as_git(&fs, b"/nowhere//x/./", false),
        Ok(Some(b"/nowhere/x".to_vec()))
    );
    assert_eq!(
        spelled_as_git(&fs, b"/a\0b", false),
        Ok(Some(b"/a\0b".to_vec()))
    );
}

#[test]
fn on_macos_the_kernel_spells_it_and_failures_are_none() {
    let fs = tree();
    assert_eq!(
        spelled_as_git(&fs, b"/w/ld/", true),
        Ok(Some(b"/w/d".to_vec()))
    );
    assert_eq!(spelled_as_git(&fs, b"/w/missing", true), Ok(None));
    assert_eq!(
        spelled_as_git(&fs, b"/w/a\0b", true),
        Err(Refused::Raised(PyErr::Value))
    );
    let mut long = MemFs::new(Flavour::Posix);
    let deep = format!("/{}", ["n"; 600].join("/"));
    long.insert(deep.as_bytes(), Node::Dir);
    long.path_max = 4096;
    // F_GETPATH's buffer is 1,024 bytes: ENOSPC past it, which is None.
    assert_eq!(spelled_as_git(&long, deep.as_bytes(), true), Ok(None));
    let no = Short {
        inner: tree(),
        cap: 1,
        offers: false,
    };
    assert_eq!(spelled_as_git(&no, b"/w/d", true), Err(Refused::NotOffered));
}
