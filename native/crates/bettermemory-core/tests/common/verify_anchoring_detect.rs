//! What the drift reader tests share (`golden_verify_anchoring_detect`,
//! the kills and robustness tests): the two rules
//! `gen_verify_anchoring_detect` adds to those of the passes, one call of
//! `detect_path_drift` over them, the report as the generator records it,
//! and the hand-written repositories in memory.

use bettermemory_core::attestation_paths::{Paths, Raised};
use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::Env;
use bettermemory_core::gitobjects::files::reader::Fault;
use bettermemory_core::gitobjects::files::Stop;
use bettermemory_core::gitobjects::Raised as ObjRaised;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::detect::{
    detect_path_drift, holds_commit, DriftError, DriftReads, Effects,
};
use bettermemory_core::verify_anchoring::passes::{ClaimReads, ReadAnchor};
use bettermemory_core::verify_anchoring::report::DictField;
use bettermemory_core::verify_anchoring::report::PathDriftReport;
use bettermemory_core::verify_candidates::scan::{ScanError, UserHome};
use serde_json::{json, Map, Value};

use super::attestation_paths::{cps, raised, text, Scripted};
use super::verify_anchoring_passes::{has, Logged, Stub};

/// The generator's `_DReads`: the passes' rules, the route rule (``t``)
/// and the live rule (``l``).
#[derive(Default)]
pub struct DLogged {
    pub inner: Logged,
}

impl ClaimReads for DLogged {
    fn key(&mut self, p: &[u32]) -> PyStr {
        self.inner.key(p)
    }
    fn path_exists(&mut self, p: &[u32]) -> bool {
        self.inner.path_exists(p)
    }
    fn absent_by_sparsity(&mut self, p: &[u32]) -> bool {
        self.inner.absent_by_sparsity(p)
    }
    fn resolves_under(&mut self, p: &[u32], root: &[u32]) -> Result<bool, Raised> {
        self.inner.resolves_under(p, root)
    }
    fn isdir(&mut self, p: &[u32]) -> bool {
        self.inner.isdir(p)
    }
}

impl DLogged {
    fn entry(&mut self, c: char, p: &[u32]) {
        let mut e = vec![u32::from(c)];
        e.extend_from_slice(p);
        self.inner.log.push(e);
    }
}

impl DriftReads for DLogged {
    fn routelike(&mut self, p: &[u32]) -> bool {
        self.entry('t', p);
        p.starts_with(&cps("/api/"))
    }
    fn root_is_live(&mut self, root: &[u32]) -> Result<bool, Raised> {
        self.entry('l', root);
        Ok(!has(root, "dead"))
    }
}

/// The generator's `_extended`: the stub anchor with a path holding `mv`
/// moved below `/api/m` and one holding a space moved below `/S` with
/// `ok` cut to `k`.
pub struct Extended(pub Stub);

impl ReadAnchor for Extended {
    fn root(&self) -> Option<PyStr> {
        self.0.root()
    }
    fn rule(&self) -> String {
        self.0.rule()
    }
    fn convicting(&self) -> bool {
        self.0.convicting()
    }
    fn absolute(&self, p: &[u32]) -> (Option<PyStr>, String) {
        if has(p, "mv") {
            let mut out = cps("/api/m");
            out.extend_from_slice(p);
            return (Some(out), "home_checkout".into());
        }
        if p.contains(&0x20) {
            let mut out = cps("/S");
            let mut i = 0;
            while i < p.len() {
                if p[i..].starts_with(&cps("ok")) {
                    out.push(u32::from('k'));
                    i += 2;
                } else {
                    out.push(p[i]);
                    i += 1;
                }
            }
            return (Some(out), "home_checkout".into());
        }
        self.0.absolute(p)
    }
}

/// The class name of what a drift read raised, as `call` spells it.
pub fn error_text(e: DriftError) -> String {
    match e {
        DriftError::Raised(r) => text(&raised(r)),
        DriftError::Scan(ScanError::Index(_)) => "!IndexError".into(),
        DriftError::Scan(ScanError::Home(_)) => "!RuntimeError".into(),
        DriftError::Scan(ScanError::NotPorted) => panic!("declined"),
    }
}

/// The report as the generator's `_report` records it.
pub fn report_json(r: &PathDriftReport) -> Value {
    let mut m = Map::new();
    for (k, v) in r.to_dict().unwrap() {
        let v = match v {
            DictField::Paths(p) => json!(p),
            DictField::Text(t) => json!(t),
            DictField::Unconfirmed(u) => {
                json!(u
                    .into_iter()
                    .map(|(p, r)| json!([p, r]))
                    .collect::<Vec<_>>())
            }
        };
        m.insert(k.to_owned(), v);
    }
    Value::Object(m)
}

/// One `detect_path_drift` call over the rules, `HOME` at `/h` and no
/// named user known: the outcome and the log.
pub fn drift(
    nt: bool,
    body: &[u32],
    vp: &Paths,
    ap: &Paths,
    root: Option<&[u32]>,
    anchor: Option<&dyn ReadAnchor>,
    spans: &[(usize, usize)],
) -> (Result<PathDriftReport, DriftError>, DLogged) {
    let host = Scripted { nt };
    let mut reads = DLogged::default();
    let home = cps("/h");
    let mut lookup = |_: &[u32]| UserHome::Unknown;
    let matches = |_: &[u32]| spans.to_vec();
    let mut fx = Effects {
        host: &host,
        reads: &mut reads,
        home: Some(&home),
        lookup: &mut lookup,
        matches: &matches,
    };
    let r = detect_path_drift(&mut fx, body, vp, ap, root, anchor);
    (r, reads)
}

/// Where the generator's scratch root stands in memory.
pub const W: &str = "/w";

/// The repositories of a ``w`` row, below [`W`].
pub fn repos(files: &Map<String, Value>) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/", Node::Dir);
    let dir = |fs: &mut MemFs, path: &str| {
        let mut at = String::new();
        for part in path.split('/').filter(|p| !p.is_empty()) {
            at.push('/');
            at.push_str(part);
            fs.insert(at.as_bytes(), Node::Dir);
        }
    };
    for (rel, hex) in files {
        let path = format!("{W}/{rel}");
        if let Some(d) = path.strip_suffix('/') {
            dir(&mut fs, d);
            continue;
        }
        let (parent, _) = path.rsplit_once('/').unwrap();
        dir(&mut fs, parent);
        fs.insert(path.as_bytes(), Node::File(unhex(hex.as_str().unwrap())));
    }
    fs
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

/// `holds_commit` at `root` below [`W`] (the root itself when empty):
/// the answer or the class name of the raise.
pub fn holds(fs: &MemFs, root: &str, sha: &str) -> Value {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    let git = GitFacts {
        release: Some((2, 54)),
        reads_compat: Some(true),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &git,
    };
    let at = if root.is_empty() {
        W.to_owned()
    } else {
        format!("{W}/{root}")
    };
    match holds_commit(&rule, &Env::default(), at.as_bytes(), sha) {
        Ok(b) => json!(b),
        Err(Fault::Overflow) => json!("!OverflowError"),
        Err(Fault::Key(_)) => json!("!KeyError"),
        Err(Fault::Raised(ObjRaised::IndexError)) => json!("!IndexError"),
        Err(Fault::Raised(ObjRaised::StructError)) => json!("!error"),
        Err(Fault::Raised(ObjRaised::ValueError) | Fault::Stop(Stop::Raised(_))) => {
            panic!("caught")
        }
        Err(Fault::Stop(Stop::NotPorted)) => panic!("declined"),
    }
}
