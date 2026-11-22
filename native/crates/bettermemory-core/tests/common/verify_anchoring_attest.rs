//! What the attestation check tests share (`golden_verify_anchoring_attest`,
//! the kills and robustness tests): the stub anchor and the closure rules
//! `gen_verify_anchoring_attest` describes, and one call of the write gate
//! spelled as the generator spells it.

use bettermemory_core::attestation_paths::{Kind, Paths};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::attest::{unverifiable_attestations, AnchorView, Reads};
use std::cell::RefCell;

use super::attestation_paths::{cps, spell, spell_list, Scripted};

fn has(p: &[u32], needle: &str) -> bool {
    let n = cps(needle);
    p.windows(n.len()).any(|w| w == n.as_slice())
}

/// The generator's `_anchor(spec)`.
pub struct Stub {
    pub root: Option<PyStr>,
    pub convicting: bool,
    pub declared: Option<PyStr>,
}

impl AnchorView for Stub {
    fn root(&self) -> Option<PyStr> {
        self.root.clone()
    }
    fn convicting(&self) -> bool {
        self.convicting
    }
    fn declared_root(&self) -> Option<PyStr> {
        self.declared.clone()
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
        (Some(p.to_vec()), "standin".into())
    }
}

/// The generator's `_paths(kind, items)`.
pub fn paths(kind: &str, items: &[PyStr]) -> Paths {
    Paths {
        kind: match kind {
            "L" => Kind::Literal,
            "R" => Kind::Reference,
            _ => Kind::Plain,
        },
        items: items.to_vec(),
    }
}

/// The generator's `_keys(p)`.
pub fn keys(p: &[u32]) -> PyStr {
    if has(p, "zz") {
        return Vec::new();
    }
    if p.last() == Some(&u32::from(b'k')) {
        return p[..p.len() - 1].to_vec();
    }
    p.to_vec()
}

/// One call of `unverifiable_attestations` under the generator's rules:
/// the spelled answer and the log of closure calls.
pub fn gate(
    nt: bool,
    paths: &Paths,
    root: Option<&[u32]>,
    anchor: Option<&Stub>,
    stored: bool,
) -> (PyStr, Vec<PyStr>) {
    let log: RefCell<Vec<PyStr>> = RefCell::new(Vec::new());
    let entry = |c: char, p: &[u32]| {
        let mut e = vec![u32::from(c)];
        e.extend_from_slice(p);
        log.borrow_mut().push(e);
    };
    let norm = |p: &[u32]| -> PyStr {
        entry('n', p);
        p.iter()
            .map(|&c| {
                if c == u32::from(b'\\') {
                    u32::from(b'/')
                } else {
                    c
                }
            })
            .collect()
    };
    let exists = |p: &[u32]| -> bool {
        entry('e', p);
        has(p, "ok")
    };
    let mut sparse = |p: &[u32]| -> bool {
        entry('s', p);
        has(p, "sp")
    };
    let host = Scripted { nt };
    let out = {
        let mut reads = Reads {
            normalize_for_compare: &norm,
            path_exists: &exists,
            absent_by_sparsity: &mut sparse,
        };
        unverifiable_attestations(
            &host,
            paths,
            root,
            anchor.map(|a| a as &dyn AnchorView),
            stored,
            &mut reads,
        )
    };
    (spell(out, |v| spell_list(&v)), log.into_inner())
}
