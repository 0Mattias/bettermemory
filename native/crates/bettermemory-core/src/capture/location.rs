//! `membership_records.ObjectKey` (288-317), the records a location is
//! made of, and `location.to_location` (`location.py` 1355-1411 at
//! ececad8c): a capture as the membership model reads it. The `history`
//! reader `to_location` takes is the resolving unit's and is left out;
//! `Location`'s properties beyond `born` read `_same_path` and are left to
//! that unit too.

use std::collections::BTreeSet;

use super::{Capture, Key, Kind, Manifest, Stamp, State};
use crate::pyvalue::PyStr;

/// `ObjectKey.compare`'s answers.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sameness {
    Same,
    Different,
    Unproven,
}

impl Sameness {
    pub fn as_str(self) -> &'static str {
        match self {
            Sameness::Same => "same",
            Sameness::Different => "different",
            Sameness::Unproven => "unproven",
        }
    }
}

/// `membership_records.ObjectKey`: device, inode and birth time as text.
/// It has no text form of its own; a token spells it as a three-item list.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct ObjectKey {
    pub device: PyStr,
    pub inode: PyStr,
    pub birth: Option<PyStr>,
}

impl ObjectKey {
    pub fn from_key(k: &Key) -> ObjectKey {
        ObjectKey {
            device: k.0.clone(),
            inode: k.1.clone(),
            birth: k.2.clone(),
        }
    }

    /// `compare`: different device or inode (or no other) is different;
    /// both birth times known decide; otherwise unproven.
    pub fn compare(&self, other: Option<&ObjectKey>) -> Sameness {
        let Some(other) = other else {
            return Sameness::Different;
        };
        if (&self.device, &self.inode) != (&other.device, &other.inode) {
            return Sameness::Different;
        }
        match (&self.birth, &other.birth) {
            (Some(a), Some(b)) if a == b => Sameness::Same,
            (Some(_), Some(_)) => Sameness::Different,
            _ => Sameness::Unproven,
        }
    }

    /// `on_any_device`: the same inode, and the same birth time where both
    /// carry one.
    pub fn on_any_device(&self, inode: &[u32], birth: Option<&[u32]>) -> bool {
        if self.inode != inode {
            return false;
        }
        match (&self.birth, birth) {
            (Some(a), Some(b)) => a.as_slice() == b,
            _ => true,
        }
    }
}

/// `membership_records.ProjectFileRef`.
#[derive(Clone, Debug, PartialEq)]
pub struct ProjectFileRef {
    pub id: PyStr,
    pub name: PyStr,
    pub dir: PyStr,
    pub path: PyStr,
    pub boundary: bool,
    pub object_key: Option<ObjectKey>,
}

/// `membership_records.Location` without its `history` reader. The
/// `frozenset`s are sets, in code point order.
#[derive(Clone, Debug, PartialEq)]
pub struct Location {
    pub machine: PyStr,
    pub cwd: PyStr,
    pub state: State,
    pub root: Option<PyStr>,
    pub kind: Option<Kind>,
    pub git_dir: Option<PyStr>,
    pub object_key: Option<ObjectKey>,
    pub common_object_key: Option<ObjectKey>,
    pub root_object_key: Option<ObjectKey>,
    pub head: Option<PyStr>,
    pub branch: Option<PyStr>,
    pub ref_tips: Vec<PyStr>,
    pub remote_keys: BTreeSet<PyStr>,
    pub alias_keys: BTreeSet<PyStr>,
    pub shallow: bool,
    pub token: Option<PyStr>,
    pub token_written: Option<PyStr>,
    pub token_status: PyStr,
    pub stamps: Vec<Stamp>,
    pub ticks: Option<bool>,
    pub files: Vec<ProjectFileRef>,
    pub file_tracked: Option<bool>,
    pub evicted: bool,
    pub detail: PyStr,
    pub manifest: Option<Manifest>,
    pub session: Option<PyStr>,
}

impl Location {
    /// `Location.born`.
    pub fn born(&self) -> bool {
        self.head.is_some() || !self.ref_tips.is_empty()
    }
}

/// `to_location(cap, machine=..., session=...)`: a field-for-field copy.
/// The capture's `common_dir`, `cwd_key`, `file_errors` and `hostname`
/// are not part of a location.
pub fn to_location(cap: &Capture, machine: &[u32], session: Option<&[u32]>) -> Location {
    let key = |k: &Option<Key>| k.as_ref().map(ObjectKey::from_key);
    Location {
        machine: machine.to_vec(),
        cwd: cap.cwd.clone(),
        state: cap.state,
        root: cap.root.clone(),
        kind: cap.kind,
        git_dir: cap.git_dir.clone(),
        object_key: key(&cap.object_key),
        common_object_key: key(&cap.common_object_key),
        root_object_key: key(&cap.root_object_key),
        head: cap.head.clone(),
        branch: cap.branch.clone(),
        ref_tips: cap.ref_tips.clone(),
        remote_keys: cap.remote_keys.iter().cloned().collect(),
        alias_keys: cap.alias_keys.iter().cloned().collect(),
        shallow: cap.shallow,
        token: cap.token.clone(),
        token_written: cap.token_written.clone(),
        token_status: cap.token_status.clone(),
        stamps: cap.stamps.clone(),
        ticks: cap.ticks,
        files: cap
            .files
            .iter()
            .map(|f| ProjectFileRef {
                id: f.id.clone(),
                name: f.name.clone(),
                dir: f.dir.clone(),
                path: f.path.clone(),
                boundary: f.boundary,
                object_key: key(&f.key),
            })
            .collect(),
        file_tracked: cap.file_tracked,
        evicted: cap.evicted,
        detail: cap.detail.clone(),
        manifest: cap.manifest.clone(),
        session: session.map(<[u32]>::to_vec),
    }
}
