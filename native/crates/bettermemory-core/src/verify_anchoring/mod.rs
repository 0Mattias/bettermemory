//! `verify_anchoring.py`: whether a record's cited paths are anchored in
//! the working tree it names. Built so far: the leaves of the
//! sparse-checkout reading (unit verify_anchoring-1a), in [`sparse`]; the
//! half that reads the disk (unit verify_anchoring-1b), in [`skip`]; the
//! report and the claim anchors (unit verify_anchoring-2a), in [`report`]
//! and [`anchor`]; the attestation write gate and its predicates (unit
//! verify_anchoring-attest), in [`attest`].

pub mod anchor;
pub mod attest;
pub mod report;
pub mod skip;
pub mod sparse;
