//! The directory signature and bounded cache used by `origin.capture`.
//! The caller supplies the two clock readings, discovery and configuration
//! inputs, and owns both the cache and the configuration reader's memo.

use std::collections::{HashMap, VecDeque};
use std::sync::Arc;

use crate::fsdata::{Fs, Refused};
use crate::gitconfig::legacy::{legacy_remote_stamps, Files, Stamp};
use crate::gitconfig::paths::join;
use crate::gitconfig::remotes::{Inputs, NotPortedWhy};
use crate::gitconfig::stamped::{
    machine_config_stamps, repository_config_stamps, MachineStamps, StampedReads,
};
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::discovery::Rule;
use crate::gitfile_rule::follow::Records;
use crate::gitfile_rule::gitdir::{Ctx, Failure};
use crate::githead::discovery::{stamp, Env, Found, Host, Owners};
use crate::githead::signature::{undetermined, CtxRefs, First, Signature};
use crate::origin_gitfiles::{machine::spelled_as_git, shortening_refs, walk::capture_files};
use crate::origin_model::CapturedOrigin;
use crate::pyvalue::PyStr;
use crate::unicode::{cps, to_string};

/// `ORIGIN_CACHE_SECONDS`, on both the monotonic and wall clocks.
pub const ORIGIN_CACHE_SECONDS: f64 = 600.0;
/// `_ORIGIN_CACHE_CAP`, in resolved directories.
pub const ORIGIN_CACHE_CAP: usize = 256;

/// A signature slot or a fresh Python `object()` in that slot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Slot<T> {
    Value(T),
    Unknown(First),
}

/// The tuple appended only when discovery found a git directory.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Extra {
    pub machine: MachineStamps,
    pub repository: Vec<Files>,
    pub legacy: [Option<Stamp>; 2],
    pub spelled_root: Slot<Vec<u8>>,
    pub refs: Vec<Option<Stamp>>,
}

/// The base is unextended when no git directory was found. A failed
/// enrichment gets its own marker inside the extension.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CaptureSignature {
    pub base: Signature,
    pub extra: Option<Slot<Extra>>,
}

/// A dependency declined the read, or discovery raised before the
/// reference entered its `try`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SignatureError {
    Discovery(&'static str),
    Configuration(NotPortedWhy),
    Filesystem,
}

/// The machine and repository configuration reads for one signature.
/// `now` is the monotonic reading used by machine_config_stamps.
pub struct Config<'a, 'i> {
    pub reads: &'a mut StampedReads,
    pub inputs: &'a Inputs<'i>,
    pub reuse: bool,
    pub now: f64,
    pub darwin: bool,
}

enum ReadError {
    Raised,
    Unavailable(SignatureError),
}

impl From<NotPortedWhy> for ReadError {
    fn from(why: NotPortedWhy) -> Self {
        match why {
            NotPortedWhy::Raised => Self::Raised,
            other => Self::Unavailable(SignatureError::Configuration(other)),
        }
    }
}

fn extra<F: Fs>(
    ctx: &Ctx<'_, F>,
    found: &Found,
    config: Config<'_, '_>,
) -> Result<Extra, ReadError> {
    let gd = &found.gd;
    let machine = machine_config_stamps(
        config.reads,
        ctx.fs,
        config.inputs,
        &gd.gitdir,
        config.reuse,
        config.now,
    )?;
    let repository = repository_config_stamps(
        config.reads,
        ctx.fs,
        config.inputs,
        &gd.commondir,
        &gd.gitdir,
    )?;
    let legacy = legacy_remote_stamps(ctx.fs, ctx.flavour, &gd.commondir, b"origin")
        .map_err(|_| ReadError::Unavailable(SignatureError::Filesystem))?;
    let spelled_root = match spelled_as_git(ctx.fs, &found.worktree_root, config.darwin) {
        Ok(Some(root)) => Slot::Value(root),
        Ok(None) => Slot::Unknown(undetermined()),
        Err(Refused::Raised(_)) => return Err(ReadError::Raised),
        Err(Refused::NotOffered) => return Err(ReadError::Unavailable(SignatureError::Filesystem)),
    };
    let mut refs = Vec::new();
    if let Some(head) = crate::githead::head_ref(&CtxRefs(ctx), gd) {
        let common = (gd.gitdir != gd.commondir).then_some(&gd.commondir);
        for name in shortening_refs(&cps(&head)) {
            // head_ref decodes strict UTF-8; shortening adds only ASCII.
            let name = to_string(&name).expect("a shortened UTF-8 ref");
            for dir in std::iter::once(&gd.gitdir).chain(common) {
                let path = join(ctx.flavour, dir, name.as_bytes());
                refs.push(match stamp(ctx, &path, false) {
                    Ok(st) => st,
                    Err(Failure::Os(_) | Failure::Value) => return Err(ReadError::Raised),
                    Err(_) => return Err(ReadError::Unavailable(SignatureError::Filesystem)),
                });
            }
        }
    }
    Ok(Extra {
        machine,
        repository,
        legacy,
        spelled_root,
        refs,
    })
}

/// `_capture_signature(start, reuse=...)`. Discovery decides the base
/// before any configuration read. Errors in the extended read replace
/// the entire extension with one fresh marker.
pub fn capture_signature<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    start: &[u8],
    config: Config<'_, '_>,
) -> Result<CaptureSignature, SignatureError> {
    let (base, found) = capture_files(rule, host, env, start).map_err(SignatureError::Discovery)?;
    let extra = match found {
        None => None,
        Some(found) => Some(match extra(rule.ctx, &found, config) {
            Ok(fields) => Slot::Value(fields),
            Err(ReadError::Raised) => Slot::Unknown(undetermined()),
            Err(ReadError::Unavailable(reason)) => return Err(reason),
        }),
    };
    Ok(CaptureSignature { base, extra })
}

/// One saved capture with its original clocks and alternate remote spellings.
#[derive(Clone, Debug, PartialEq)]
pub struct OriginEntry {
    pub origin: CapturedOrigin,
    pub alternates: Vec<PyStr>,
    pub signature: CaptureSignature,
    pub captured_at: f64,
    pub captured_wall: f64,
}

impl OriginEntry {
    /// The three hit tests in `capture`, including a backwards clock.
    pub fn matches(&self, now: f64, wall: f64, signature: &CaptureSignature) -> bool {
        (0.0..ORIGIN_CACHE_SECONDS).contains(&(now - self.captured_at))
            && (0.0..ORIGIN_CACHE_SECONDS).contains(&(wall - self.captured_wall))
            && self.signature == *signature
    }
}

/// Entries keyed by directory and ordered by the last insertion. Each
/// key's text is shared between the index and insertion order.
#[derive(Default)]
pub struct OriginCache {
    entries: HashMap<Arc<[u32]>, OriginEntry>,
    order: VecDeque<Arc<[u32]>>,
}

impl OriginCache {
    /// A lookup leaves the eviction order intact.
    pub fn lookup(&self, key: &[u32]) -> Option<&OriginEntry> {
        self.entries.get(key)
    }

    /// `_remember`: replace as newest and drop the oldest entry past the cap.
    pub fn remember(&mut self, key: PyStr, entry: OriginEntry) {
        let key = match self.entries.remove_entry(key.as_slice()) {
            Some((stored, _)) => {
                self.order.retain(|k| !Arc::ptr_eq(k, &stored));
                stored
            }
            None => Arc::from(key),
        };
        self.entries.insert(key.clone(), entry);
        self.order.push_back(key);
        if self.order.len() > ORIGIN_CACHE_CAP {
            let oldest = self.order.pop_front().expect("an entry over the cap");
            self.entries.remove(&oldest);
        }
    }

    /// `_clear_origin_cache`.
    pub fn clear(&mut self) {
        self.entries.clear();
        self.order.clear();
    }
}
