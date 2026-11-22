//! The v8 file names and frontmatter the mirror writes
//! (`mirror_format.py` whole): the layout, the names a record's file and
//! its tombstone were given, the tombstone name parser, and the
//! frontmatter mappings `memory_metadata` and `episode_metadata`.
//!
//! `_ID_TOMBSTONE_RE` is matched by hand ([`id_tombstone_stem`]): an
//! anchored literal suffix and a fixed 26-character class.

use crate::identity::SOURCE_PROCESS_CWD;
use crate::models_text::{build_filename, make_slug, SLUG_MAX_CHARS, SLUG_MAX_WORDS};
use crate::native_text::origin_projection;
use crate::pyvalue::{PyStr, PyValue};
use crate::records::{Episode, Memory, Origin};
use crate::store::SCHEMA_VERSION;
use crate::unicode;

pub const TOMBSTONE_DIR: &str = ".tombstones";
pub const EPISODES_DIR: &str = "episodes";
pub const TOMBSTONE_SUFFIX: &str = ".tombstone.md";

pub const MEMORY_METADATA_KEYS: [&str; 20] = [
    "schema_version",
    "id",
    "created",
    "updated",
    "scopes",
    "confidence",
    "source",
    "origin",
    "actor",
    "last_verified_at",
    "category",
    "corroborations",
    "last_corroborated",
    "verified_paths",
    "verified_commits",
    "verified_versions",
    "verified_absent_paths",
    "claims",
    "verified_head",
    "links",
];
pub const PATH_TARGET_METADATA_KEYS: [&str; 2] =
    ["verified_path_targets", "verified_absent_path_targets"];
pub const TOMBSTONE_METADATA_KEYS: [&str; 3] = ["removed", "removed_reason", "removed_session"];
pub const EPISODE_METADATA_KEYS: [&str; 9] = [
    "schema_version",
    "id",
    "session_id",
    "created",
    "scopes",
    "takeaway",
    "origin",
    "is_floor",
    "swarm_id",
];

const NL: u32 = 0x0A;
const ID_LEN: usize = 26;

fn crockford(cp: u32) -> bool {
    matches!(cp, 0x30..=0x39 | 0x41..=0x48 | 0x4A | 0x4B | 0x4D | 0x4E | 0x50..=0x54 | 0x56..=0x5A)
}

/// `_ID_TOMBSTONE_RE.match(name)`'s `stem` group, or None where it does
/// not match. `$` also matches before one final newline, and `.` matches
/// no newline, so any other newline rejects. The suffix is anchored, so
/// the id is the 26 characters before it and the stem the rest.
pub fn id_tombstone_stem(name: &[u32]) -> Option<&[u32]> {
    let s = match name.split_last() {
        Some((&NL, rest)) => rest,
        _ => name,
    };
    if s.contains(&NL) {
        return None;
    }
    let suffix = unicode::cps(TOMBSTONE_SUFFIX);
    let body = s.strip_suffix(suffix.as_slice())?;
    if body.len() < ID_LEN + 2 {
        return None;
    }
    let (head, id) = body.split_at(body.len() - ID_LEN);
    if !id.iter().all(|&c| crockford(c)) {
        return None;
    }
    let (stem, dot) = head.split_at(head.len() - 1);
    (dot == [u32::from(b'.')]).then_some(stem)
}

/// `memory_filename(memory)`: the date, the slug of the body and the
/// lowercased id.
pub fn memory_filename(memory: &Memory) -> PyStr {
    let f = &memory.fields;
    let mut slug = make_slug(&f.body, SLUG_MAX_WORDS, SLUG_MAX_CHARS);
    slug.push(u32::from(b'-'));
    slug.extend(unicode::lower(&f.id));
    build_filename(&f.created, &slug)
}

/// `tombstone_filename(active_filename, memory_id)`.
pub fn tombstone_filename(active_filename: &[u32], memory_id: &[u32]) -> PyStr {
    let md = unicode::cps(".md");
    let stem = active_filename
        .strip_suffix(md.as_slice())
        .unwrap_or(active_filename);
    let mut out = stem.to_vec();
    out.push(u32::from(b'.'));
    out.extend_from_slice(memory_id);
    out.extend(unicode::cps(TOMBSTONE_SUFFIX));
    out
}

/// `active_filename_for_tombstone(name)`.
pub fn active_filename_for_tombstone(name: &[u32]) -> PyStr {
    let md = unicode::cps(".md");
    if let Some(stem) = id_tombstone_stem(name) {
        return [stem, &md].concat();
    }
    let suffix = unicode::cps(TOMBSTONE_SUFFIX);
    match name.strip_suffix(suffix.as_slice()) {
        Some(stem) => [stem, &md].concat(),
        None => name.to_vec(),
    }
}

/// `is_legacy_tombstone_name(name)`.
pub fn is_legacy_tombstone_name(name: &[u32]) -> bool {
    name.ends_with(&unicode::cps(TOMBSTONE_SUFFIX)) && id_tombstone_stem(name).is_none()
}

fn entry(key: &str, value: PyValue) -> (PyValue, PyValue) {
    (PyValue::str(key), value)
}

fn str_list(items: &[PyStr]) -> PyValue {
    PyValue::List(items.iter().map(|s| PyValue::Str(s.clone())).collect())
}

fn opt_str(v: &Option<PyStr>) -> PyValue {
    v.as_ref()
        .map_or(PyValue::None, |s| PyValue::Str(s.clone()))
}

/// The origin's projected entries and its native extension; with
/// `drop_process_cwd`, a `source` of `process-cwd` is left out.
fn origin_entries(origin: &Origin, drop_process_cwd: bool, meta: &mut Vec<(PyValue, PyValue)>) {
    let dumped = origin.dump(true);
    let PyValue::Dict(dumped) = &dumped else {
        unreachable!("an origin dumps as a mapping")
    };
    let (mut legacy, native) =
        origin_projection(dumped).expect("an origin's text fields always encode");
    if let Some(native) = native {
        meta.push(entry("native_origin", PyValue::Str(native)));
    }
    if drop_process_cwd {
        let source = PyValue::str("source");
        let cwd = PyValue::str(SOURCE_PROCESS_CWD);
        if let Some(i) = legacy.iter().position(|(k, v)| *k == source && *v == cwd) {
            legacy.remove(i);
        }
    }
    if !legacy.is_empty() {
        meta.push(entry("origin", PyValue::Dict(legacy)));
    }
}

/// `memory_metadata(memory)`: the frontmatter mapping, in insertion order.
pub fn memory_metadata(memory: &Memory) -> PyValue {
    let f = &memory.fields;
    let mut meta = vec![
        entry("schema_version", PyValue::int(i128::from(SCHEMA_VERSION))),
        entry("id", PyValue::Str(f.id.clone())),
        entry("created", PyValue::DateTime(f.created)),
        entry("updated", PyValue::DateTime(f.updated)),
        entry("scopes", str_list(&f.scopes)),
        entry("confidence", PyValue::str(f.confidence.value())),
        entry("source", PyValue::str(f.source.value())),
    ];
    if let Some(origin) = &f.origin {
        origin_entries(origin, true, &mut meta);
    }
    if let Some(actor) = &f.actor {
        let record = actor.to_record();
        if !matches!(&record, PyValue::Dict(items) if items.is_empty()) {
            meta.push(entry("actor", record));
        }
    }
    if let Some(at) = &f.last_verified_at {
        meta.push(entry("last_verified_at", PyValue::DateTime(*at)));
    }
    if let Some(category) = f.category {
        meta.push(entry("category", PyValue::str(category.value())));
    }
    if memory.corroborations != "0" {
        meta.push(entry(
            "corroborations",
            PyValue::Int(memory.corroborations.clone()),
        ));
    }
    if let Some(at) = &memory.last_corroborated {
        meta.push(entry("last_corroborated", PyValue::DateTime(*at)));
    }
    let lists = [
        ("verified_paths", &f.verified_paths),
        ("verified_commits", &f.verified_commits),
        ("verified_versions", &f.verified_versions),
        ("verified_absent_paths", &f.verified_absent_paths),
    ];
    for (key, items) in lists {
        if !items.is_empty() {
            meta.push(entry(key, str_list(items)));
        }
    }
    let targets = [&f.verified_path_targets, &f.verified_absent_path_targets];
    for (key, targets) in PATH_TARGET_METADATA_KEYS.iter().zip(targets) {
        if let Some(items) = targets {
            let dict = items
                .iter()
                .map(|(k, v)| (PyValue::Str(k.clone()), opt_str(v)))
                .collect();
            meta.push(entry(key, PyValue::Dict(dict)));
        }
    }
    if !f.claims.is_empty() {
        meta.push(entry("claims", str_list(&f.claims)));
    }
    if let Some(head) = f.verified_head.as_ref().filter(|h| !h.is_empty()) {
        meta.push(entry("verified_head", PyValue::Str(head.clone())));
    }
    if !memory.links.is_empty() {
        let links = memory
            .links
            .iter()
            .map(|link| {
                let mut d = vec![
                    entry("type", PyValue::str(link.link_type.value())),
                    entry("target_id", PyValue::Str(link.target_id.clone())),
                ];
                if let Some(note) = &link.note {
                    d.push(entry("note", PyValue::Str(note.clone())));
                }
                PyValue::Dict(d)
            })
            .collect();
        meta.push(entry("links", PyValue::List(links)));
    }
    PyValue::Dict(meta)
}

/// `episode_metadata(episode)`: the frontmatter mapping, in insertion
/// order. Unlike a memory's, a `process-cwd` source is kept.
pub fn episode_metadata(episode: &Episode) -> PyValue {
    let mut meta = vec![
        entry("schema_version", PyValue::int(i128::from(SCHEMA_VERSION))),
        entry("id", PyValue::Str(episode.id.clone())),
        entry("session_id", PyValue::Str(episode.session_id.clone())),
        entry("created", PyValue::DateTime(episode.created)),
    ];
    if !episode.scopes.is_empty() {
        meta.push(entry("scopes", str_list(&episode.scopes)));
    }
    if let Some(takeaway) = &episode.takeaway {
        meta.push(entry("takeaway", PyValue::Str(takeaway.clone())));
    }
    if let Some(origin) = &episode.origin {
        origin_entries(origin, false, &mut meta);
    }
    if episode.is_floor {
        meta.push(entry("is_floor", PyValue::Bool(true)));
    }
    if let Some(swarm) = &episode.swarm_id {
        meta.push(entry("swarm_id", PyValue::Str(swarm.clone())));
    }
    PyValue::Dict(meta)
}
