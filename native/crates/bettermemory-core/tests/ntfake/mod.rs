//! The fake `nt` of `gen_ntrealpath`: each Windows call answers from the
//! case's map, a path not there raising the map's default winerror (2 for
//! `_getfinalpathname` and `_findfirstfile`, 4390 for `readlink`, 123 for
//! `_getfullpathname`); `islink` admits the case's links.

#![allow(dead_code)]

use std::collections::HashMap;

use bettermemory_core::fsdata::realpath::{FinalPath, NtRaise};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use serde_json::Value;

/// The generator's fake `nt`: each call answers from its map, a path not
/// there raising the map's default winerror.
pub struct Table {
    /// How many calls were made, of every kind.
    pub calls: std::cell::Cell<usize>,
    maps: HashMap<&'static str, HashMap<PyStr, Value>>,
    links: Vec<PyStr>,
}

impl Table {
    pub fn of(t: &Value) -> Table {
        let mut maps = HashMap::new();
        for key in ["f", "r", "ff", "p"] {
            let map = t[key]
                .as_object()
                .map(|m| m.iter().map(|(k, v)| (cps(k), v.clone())).collect())
                .unwrap_or_default();
            maps.insert(key, map);
        }
        let links = t["l"]
            .as_array()
            .map(|l| l.iter().map(|v| cps(v.as_str().unwrap())).collect())
            .unwrap_or_default();
        Table {
            calls: std::cell::Cell::new(0),
            maps,
            links,
        }
    }

    fn look(&self, key: &str, default: i64, path: &[u32]) -> Result<PyStr, NtRaise> {
        self.calls.set(self.calls.get() + 1);
        match self.maps[key].get(path) {
            None => Err(NtRaise::Os(Some(default))),
            Some(Value::Null) => Err(NtRaise::Value),
            Some(Value::String(s)) => Ok(cps(s)),
            Some(v) => Err(NtRaise::Os(v.as_i64())),
        }
    }
}

impl FinalPath for Table {
    fn final_path_name(&self, path: &[u32]) -> Result<PyStr, NtRaise> {
        self.look("f", 2, path)
    }
    fn full_path_name(&self, path: &[u32]) -> Option<PyStr> {
        self.look("p", 123, path).ok()
    }
    fn readlink(&self, path: &[u32]) -> Result<PyStr, NtRaise> {
        self.look("r", 4390, path)
    }
    fn find_first_file(&self, path: &[u32]) -> Result<PyStr, NtRaise> {
        self.look("ff", 2, path)
    }
    fn is_link(&self, path: &[u32]) -> bool {
        self.links.iter().any(|l| l == path)
    }
}
