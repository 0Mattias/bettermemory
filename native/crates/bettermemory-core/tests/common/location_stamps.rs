//! `location_stamps.jsonl` as data: a file system that answers `stat`,
//! the mount table's reads and FILE_BASIC_INFO from tables, the table
//! spellings, and the sweeps' SplitMix64.

use std::collections::BTreeMap;

use bettermemory_core::fsdata::{Access, BasicInfo, Fs, Kind, OsError, PyErr, Stat, StatResult};
use serde_json::Value;

/// A file's bytes, `Err` for a folder.
pub type Contents = Result<Vec<u8>, ()>;

/// A file system as the case's tables: `stat` by path, one file read in
/// chunks (or a folder, or nothing), FILE_BASIC_INFO by path.
#[derive(Default)]
pub struct TableFs {
    pub stats: BTreeMap<Vec<u8>, Result<StatResult, PyErr>>,
    /// The mount table's path and its bytes, `Err` for a folder.
    pub file: Option<(Vec<u8>, Contents)>,
    pub basic: BTreeMap<Vec<u8>, BasicInfo>,
    /// Whether `basic_info` declines (the default) where `basic` has no
    /// entry; else it fails.
    pub basic_fails: bool,
    /// How many reads of the mount table succeed before every later one
    /// fails; None for no failure.
    pub reads_ok: Option<usize>,
}

pub struct TableFile {
    data: Result<Vec<u8>, ()>,
    at: usize,
    left: Option<usize>,
}

pub fn stat(ino: u64, dev: u64, mtime: i128, ctime: i128, size: u64) -> StatResult {
    StatResult {
        kind: Kind::Regular,
        mode: Some(0o100644),
        ino: Some(ino),
        dev: Some(dev),
        size,
        mtime_ns: mtime,
        ctime_ns: Some(ctime),
        flags: 0,
        file_attributes: 0,
        reparse_tag: Some(0),
    }
}

impl Fs for TableFs {
    type File = TableFile;

    fn open(&self, path: &[u8]) -> Result<TableFile, PyErr> {
        match &self.file {
            Some((p, data)) if p == path => Ok(TableFile {
                data: data.clone(),
                at: 0,
                left: self.reads_ok,
            }),
            _ => Err(PyErr::FileNotFound),
        }
    }

    fn fstat(&self, _file: &mut TableFile) -> Result<Stat, PyErr> {
        unimplemented!()
    }

    fn read(&self, file: &mut TableFile, max: usize) -> Result<Vec<u8>, PyErr> {
        match &mut file.left {
            Some(0) => return Err(PyErr::Os),
            Some(n) => *n -= 1,
            None => {}
        }
        let data = file.data.as_ref().map_err(|()| PyErr::IsADirectory)?;
        let start = file.at.min(data.len());
        let end = start.saturating_add(max).min(data.len());
        file.at = end;
        Ok(data[start..end].to_vec())
    }

    fn access(&self, _path: &[u8], _mode: Access) -> Result<bool, PyErr> {
        unimplemented!()
    }

    fn open_no_follow(&self, _path: &[u8]) -> Result<TableFile, OsError> {
        unimplemented!()
    }

    fn lstat(&self, _path: &[u8]) -> Result<Kind, PyErr> {
        unimplemented!()
    }

    fn readlink(&self, _path: &[u8]) -> Result<Vec<u8>, PyErr> {
        unimplemented!()
    }

    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        match self.stats.get(path) {
            Some(Ok(st)) => Ok(st.clone()),
            Some(Err(PyErr::Value)) => Err(OsError::value()),
            Some(Err(class)) => Err(OsError::new(*class, "EACCES")),
            None => Err(OsError::new(PyErr::FileNotFound, "ENOENT")),
        }
    }

    fn lstat_result(&self, _path: &[u8]) -> Result<StatResult, OsError> {
        unimplemented!()
    }

    fn listdir(&self, _path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        unimplemented!()
    }

    fn basic_info(&self, path: &[u8]) -> BasicInfo {
        match self.basic.get(path) {
            Some(info) => *info,
            None if self.basic_fails => BasicInfo::Failed,
            None => BasicInfo::Declined,
        }
    }
}

/// The generator's `_b`, read back.
pub fn bytes_of(spelled: &str) -> Vec<u8> {
    match spelled.strip_prefix('=') {
        Some(text) => text.as_bytes().to_vec(),
        None => super::unhex(spelled),
    }
}

/// The generator's `_t`, read back.
pub fn table_of(pieces: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    for piece in pieces.as_array().expect("pieces") {
        match piece {
            Value::String(s) => out.extend(bytes_of(s)),
            Value::Array(run) => {
                let byte = bytes_of(run[0].as_str().unwrap());
                let n = run[1].as_u64().unwrap() as usize;
                out.extend(std::iter::repeat_n(byte[0], n));
            }
            other => panic!("not a piece: {other}"),
        }
    }
    out
}

/// A decimal string as an `i128`.
pub fn int(value: &Value) -> i128 {
    value
        .as_str()
        .expect("a decimal string")
        .parse()
        .expect("an i128")
}

/// The generator's `splitmix`.
pub struct SplitMix(pub u64);

impl Iterator for SplitMix {
    type Item = u64;
    fn next(&mut self) -> Option<u64> {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        Some(z ^ (z >> 31))
    }
}

/// The generator's `_sweep_times`.
pub fn sweep_times(gen: &mut SplitMix) -> Vec<i128> {
    let n = gen.next().unwrap() % 5;
    (0..n)
        .map(|_| {
            let x = gen.next().unwrap();
            let mag = i128::from((x >> 8) % 2001) - 1000;
            mag * [1, 100, 1_000_000_000][(x % 3) as usize]
        })
        .collect()
}
