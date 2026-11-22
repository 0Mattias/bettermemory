//! What the read pass tests share (`golden_verify_anchoring_passes`, the
//! kills and robustness tests): the closure rules and the stub anchor
//! `gen_verify_anchoring_passes` describes, the buckets as it records
//! them, and one call of each pass.

use bettermemory_core::attestation_paths::{Paths, Raised};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::passes::{
    check_anchored_attestations, check_anchored_citations, read_attestations, read_citations,
    AnchoredRead, ClaimReads, ReadAnchor,
};
use serde_json::{json, Value};

use super::attestation_paths::{cps, text, Scripted};

pub fn has(p: &[u32], needle: &str) -> bool {
    let n = cps(needle);
    p.windows(n.len()).any(|w| w == n.as_slice())
}

/// The generator's `_Reads`: each effect logged with its initial.
#[derive(Default)]
pub struct Logged {
    pub log: Vec<PyStr>,
    pub keys: usize,
}

impl Logged {
    fn entry(&mut self, c: char, p: &[u32]) {
        let mut e = vec![u32::from(c)];
        e.extend_from_slice(p);
        self.log.push(e);
    }
}

impl ClaimReads for Logged {
    fn key(&mut self, p: &[u32]) -> PyStr {
        self.keys += 1;
        if has(p, "zz") {
            return Vec::new();
        }
        p.iter()
            .map(|&c| if (65..=90).contains(&c) { c + 32 } else { c })
            .collect()
    }
    fn path_exists(&mut self, p: &[u32]) -> bool {
        self.entry('e', p);
        has(p, "ok")
    }
    fn absent_by_sparsity(&mut self, p: &[u32]) -> bool {
        self.entry('s', p);
        has(p, "sp")
    }
    fn resolves_under(&mut self, p: &[u32], root: &[u32]) -> Result<bool, Raised> {
        self.entry('r', p);
        Ok(p.starts_with(root))
    }
    fn isdir(&mut self, p: &[u32]) -> bool {
        self.entry('d', p);
        !has(p, "nod")
    }
}

/// The generator's `_anchor(spec)`.
pub struct Stub {
    pub root: Option<PyStr>,
    pub rule: String,
    pub convicting: bool,
}

impl Stub {
    pub fn from_json(a: &Value) -> Stub {
        Stub {
            root: a[0].as_str().map(cps),
            rule: a[1].as_str().unwrap().to_owned(),
            convicting: a[2].as_bool().unwrap(),
        }
    }
}

impl ReadAnchor for Stub {
    fn root(&self) -> Option<PyStr> {
        self.root.clone()
    }
    fn rule(&self) -> String {
        self.rule.clone()
    }
    fn convicting(&self) -> bool {
        self.convicting
    }
    fn absolute(&self, p: &[u32]) -> (Option<PyStr>, String) {
        if has(p, "un") {
            return (None, "unanchored".into());
        }
        if has(p, "hq") {
            let mut out = cps("/H");
            out.extend_from_slice(p);
            return (Some(out), "home_checkout".into());
        }
        if has(p, "ab") {
            return (Some(p.to_vec()), "absolute".into());
        }
        (Some(p.to_vec()), "standin".into())
    }
}

fn list(items: &[PyStr]) -> Value {
    json!(items.iter().map(|s| text(s)).collect::<Vec<_>>())
}

/// The generator's `_buckets(out)`.
pub fn buckets(out: &AnchoredRead) -> Value {
    json!({
        "checked": list(&out.checked),
        "missing": list(&out.missing),
        "claim": list(&out.claim_anchored),
        "verified": list(&out.verified),
        "absent": list(&out.expected_absent),
        "unconfirmed": list(&out.unconfirmed),
        "reasons": json!(out.reasons),
        "present_absent": list(&out.present_absent),
    })
}

pub fn log_json(log: &[PyStr]) -> Value {
    list(log)
}

/// An `_AnchoredRead` with `pre` read first.
pub fn seeded(reads: &mut Logged, pre: &[PyStr]) -> AnchoredRead {
    let mut out = AnchoredRead::default();
    for p in pre {
        out.first(reads, p);
    }
    out
}

/// Which pass a call runs.
pub enum Pass<'a> {
    ReadAttestations(&'a Stub, &'a Paths, &'a Paths),
    CheckAttestations(Option<&'a [u32]>, &'a Paths, &'a Paths),
    ReadCitations(&'a Stub, &'a [u32]),
    CheckCitations(&'a [u32], &'a [u32]),
}

/// One pass over the generator's rules, the citations at `spans`: what
/// it raised, the buckets and the log.
pub fn run(
    nt: bool,
    pass: Pass<'_>,
    spans: &[(usize, usize)],
    pre: &[PyStr],
) -> (Result<(), Raised>, AnchoredRead, Logged) {
    let host = Scripted { nt };
    let mut reads = Logged::default();
    let mut out = seeded(&mut reads, pre);
    let matches = |_: &[u32]| spans.to_vec();
    let r = match pass {
        Pass::ReadAttestations(a, vp, ap) => {
            read_attestations(&host, &mut reads, a, vp, ap, &mut out)
        }
        Pass::CheckAttestations(root, vp, ap) => {
            check_anchored_attestations(&host, &mut reads, root, vp, ap, &mut out)
        }
        Pass::ReadCitations(a, body) => {
            read_citations(&host, &mut reads, a, body, &matches, &mut out)
        }
        Pass::CheckCitations(root, body) => {
            check_anchored_citations(&host, &mut reads, root, body, &matches, &mut out)
        }
    };
    (r, out, reads)
}
