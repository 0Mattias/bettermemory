//! Answers of `gitobjects::files::reader` that a plausible wrong port
//! misses, each as the reference gives it.

mod common;

use bettermemory_core::fsdata::{
    Access, Fs, Kind, MemFs, Node, OsError, PyErr, Refused, Stat, StatResult,
};
use bettermemory_core::gitobjects::files::reader::{Fault, MAX_OBJECT_BYTES};
use common::gitobjects_reader::{
    empty_repo, entry_head, index_of, loose_file, loose_path, name_of, reader_of, stored,
    unhex_text,
};

const OBJ: &str = "/m/g/objects";

fn with_loose(fs: &mut MemFs, name: &str, kind: &str, body: &[u8]) {
    fs.insert(&loose_path(OBJ, name), Node::File(loose_file(kind, body)));
}

fn with_pack(fs: &mut MemFs, stem: &str, pack: Vec<u8>, entries: &[(String, u64)]) {
    let pairs: Vec<(Vec<u8>, u64)> = entries.iter().map(|(n, at)| (unhex_text(n), *at)).collect();
    fs.insert(
        format!("{OBJ}/pack/{stem}.pack").as_bytes(),
        Node::File(pack),
    );
    fs.insert(
        format!("{OBJ}/pack/{stem}.idx").as_bytes(),
        Node::File(index_of(&pairs, 20)),
    );
}

fn blob_pack(body: &[u8]) -> Vec<u8> {
    let mut pack = b"PACK\0\0\0\x02\0\0\0\x01".to_vec();
    pack.extend(entry_head(3, body.len()));
    pack.extend(stored(body));
    pack
}

#[test]
fn one_object_in_a_pack_and_loose_resolves_as_one_name() {
    let name = name_of("abcd12", '0', 20);
    let mut fs = empty_repo();
    with_loose(&mut fs, &name, "blob", b"x");
    with_pack(&mut fs, "p", blob_pack(b"x"), &[(name.clone(), 12)]);
    assert_eq!(reader_of(&fs).resolve("abcd").unwrap(), Some(name));
}

#[test]
fn two_names_in_two_object_folders_are_ambiguous() {
    let one = name_of("abcd1", '0', 20);
    let two = name_of("abcd2", '0', 20);
    let mut fs = empty_repo();
    with_loose(&mut fs, &one, "blob", b"x");
    fs.insert(b"/m/x/objects", Node::Dir);
    fs.insert(
        &loose_path("/m/x/objects", &two),
        Node::File(loose_file("blob", b"y")),
    );
    fs.insert(
        b"/m/g/objects/info/alternates",
        Node::File(b"/m/x/objects\n".to_vec()),
    );
    let mut reader = reader_of(&fs);
    assert_eq!(reader.resolve("abcd").unwrap(), None);
    assert_eq!(reader.resolve("abcd1").unwrap(), Some(one));
}

#[test]
fn has_many_answers_lowercase_once() {
    let name = name_of("abcd", 'e', 20);
    let mut fs = empty_repo();
    with_loose(&mut fs, &name, "blob", b"x");
    let upper = name.to_ascii_uppercase();
    let found = reader_of(&fs).has_many(&[&upper, &name, "zz"]).unwrap();
    assert_eq!(found.into_iter().collect::<Vec<_>>(), vec![name]);
}

#[test]
fn an_unlistable_fanout_hides_two_names_but_not_one() {
    let one = name_of("ab1", '0', 20);
    let two = name_of("ab2", '0', 20);
    let mut fs = empty_repo();
    with_loose(&mut fs, &one, "blob", b"x");
    with_loose(&mut fs, &two, "blob", b"y");
    fs.set_mode(b"/m/g/objects/ab", 0o300);
    let mut reader = reader_of(&fs);
    assert!(reader.has_many(&[&one, &two]).unwrap().is_empty());
    assert!(reader.has(&one).unwrap());
    assert_eq!(reader.resolve("ab10").unwrap(), None);
}

#[test]
fn a_delta_header_states_the_target_size() {
    let base = name_of("aa", '0', 20);
    let top = name_of("bb", '0', 20);
    let mut pack = blob_pack(b"0123456789");
    let at = pack.len() as u64;
    let delta = [10u8, 12, 0x90, 10, 2, b'a', b'b'];
    pack.extend(entry_head(6, delta.len()));
    pack.push((at - 12) as u8);
    pack.extend(stored(&delta));
    let mut fs = empty_repo();
    with_pack(&mut fs, "p", pack, &[(base, 12), (top.clone(), at)]);
    let mut reader = reader_of(&fs);
    let (kind, size) = reader.header(&top).unwrap().unwrap();
    assert_eq!((kind, size.to_u64()), ("blob", Some(12)));
    assert_eq!(reader.read(&top, 12).unwrap().unwrap().1, b"0123456789ab");
    assert_eq!(reader.read(&top, 11).unwrap(), None);
}

#[test]
fn an_offset_of_2_to_the_63_raises_out_of_header() {
    let name = name_of("cc", '0', 20);
    let other = name_of("dd", '0', 20);
    let mut fs = empty_repo();
    with_pack(
        &mut fs,
        "p",
        blob_pack(b"x"),
        &[(other, 12), (name.clone(), 1 << 63)],
    );
    let mut reader = reader_of(&fs);
    assert!(reader.has(&name).unwrap());
    assert_eq!(reader.header(&name), Err(Fault::Overflow));
    assert_eq!(reader.complete(), Ok(true));
}

#[test]
fn a_pack_written_after_a_question_is_found_by_the_next() {
    use common::gitobjects_files::{Claims, Tree};
    let name = name_of("ef", '0', 20);
    let fs = Claims::new(empty_repo());
    let mut reader = reader_of(&fs);
    assert_eq!(reader.header(&name).unwrap(), None);
    let pairs = [(unhex_text(&name), 12u64)];
    MemFs::write(
        &fs,
        format!("{OBJ}/pack/p.pack").as_bytes(),
        &blob_pack(b"late"),
    );
    MemFs::write(
        &fs,
        format!("{OBJ}/pack/p.idx").as_bytes(),
        &index_of(&pairs, 20),
    );
    assert_eq!(
        reader.read(&name, MAX_OBJECT_BYTES).unwrap().unwrap().1,
        b"late"
    );
}

#[test]
fn a_prefix_is_counted_in_characters() {
    let name = name_of("abcd", '0', 20);
    let mut fs = empty_repo();
    with_loose(&mut fs, &name, "blob", b"x");
    let mut reader = reader_of(&fs);
    assert_eq!(reader.resolve("abc\u{e9}").unwrap(), None);
    assert_eq!(reader.resolve("ABCD").unwrap(), Some(name.clone()));
    assert_eq!(reader.resolve("abc").unwrap(), None);
    let full: String = name.chars().take(39).chain("\u{e9}".chars()).collect();
    assert!(!reader.has(&full).unwrap());
}

#[test]
fn a_loose_size_above_the_cap_is_refused_and_at_it_read() {
    let name = name_of("12", '0', 20);
    let mut fs = empty_repo();
    with_loose(&mut fs, &name, "blob", b"hello");
    let mut reader = reader_of(&fs);
    assert_eq!(reader.read(&name, 4).unwrap(), None);
    assert_eq!(reader.read(&name, 5).unwrap().unwrap().1, b"hello");
    assert_eq!(reader.header(&name).unwrap().unwrap().1.to_u64(), Some(5));
}

/// A file system whose reads fail with EIO once `broken` is set: the
/// open works, then the read of the object raises `OSError`.
struct FailingReads {
    inner: MemFs,
    broken: std::cell::Cell<bool>,
}

impl Fs for FailingReads {
    type File = <MemFs as Fs>::File;
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.inner.open(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        if self.broken.get() {
            return Err(PyErr::Os);
        }
        self.inner.read(file, max)
    }
    fn read_once(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, OsError> {
        if self.broken.get() {
            return Err(OsError::new(PyErr::Os, "EIO"));
        }
        self.inner.read_once(file, max)
    }
    fn seek(&self, file: &mut Self::File, offset: u64) -> Result<(), Refused> {
        self.inner.seek(file, offset)
    }
    fn close(&self, file: Self::File) -> Result<(), OsError> {
        self.inner.close(file)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.inner.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.readlink(path)
    }
    fn readlink_result(&self, path: &[u8]) -> Result<Vec<u8>, OsError> {
        self.inner.readlink_result(path)
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
}

#[test]
fn an_os_error_while_an_object_is_read_answers_absent() {
    // `_loose` and `_packed` catch OSError raised after the open has
    // worked: the reference answers None, never raises.
    let loose = name_of("a1", '0', 20);
    let packed = name_of("b1", '0', 20);
    let mut fs = empty_repo();
    with_loose(&mut fs, &loose, "blob", b"x");
    with_pack(&mut fs, "p", blob_pack(b"y"), &[(packed.clone(), 12)]);
    let fs = FailingReads {
        inner: fs,
        broken: std::cell::Cell::new(false),
    };
    let mut reader = reader_of(&fs);
    assert!(reader.has(&loose).unwrap());
    assert!(reader.has(&packed).unwrap());
    fs.broken.set(true);
    for name in [&loose, &packed] {
        assert_eq!(reader.header(name).unwrap(), None);
        assert_eq!(reader.read(name, MAX_OBJECT_BYTES).unwrap(), None);
    }
    fs.broken.set(false);
    assert_eq!(
        reader.read(&loose, MAX_OBJECT_BYTES).unwrap().unwrap().1,
        b"x"
    );
    assert_eq!(
        reader.read(&packed, MAX_OBJECT_BYTES).unwrap().unwrap().1,
        b"y"
    );
}
