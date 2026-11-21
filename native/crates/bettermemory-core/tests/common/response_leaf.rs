//! The snapshots of `response_leaf.jsonl`'s link cases, built as the
//! generator builds them (`tests/contract/gen_response_leaf.py`), and the
//! sweep's rule and spelling.

use bettermemory_core::records::{LinkType, Memory, MemoryLink};
use bettermemory_core::response_links::candidate_link_rows;
use bettermemory_core::unicode;

use super::written;

/// Each memory: its short id and its links (type, short target, note).
pub type Snapshot = Vec<(u64, Vec<(String, u64, Option<String>)>)>;
/// Each wanted id: its short id, outbound and inbound edges.
pub type Answer = Vec<(u64, Vec<Edge>, Vec<Edge>)>;
pub type Edge = (String, u64, Option<String>);

const CROCKFORD: &[u8] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const KINDS: [&str; 4] = ["supersedes", "contradicts", "extends", "depends_on"];

/// `_uid`: ``01J`` and `n` in 23 base-32 digits.
pub fn uid(n: u64) -> String {
    let mut n = n;
    let mut digits = Vec::new();
    for _ in 0..23 {
        digits.push(CROCKFORD[(n % 32) as usize]);
        n /= 32;
    }
    digits.reverse();
    format!("01J{}", String::from_utf8(digits).expect("ASCII"))
}

fn short(id: &[u32]) -> u64 {
    let text = unicode::to_string(id).expect("an id");
    text.bytes().skip(3).fold(0, |n, b| {
        n * 32 + CROCKFORD.iter().position(|c| *c == b).expect("a digit") as u64
    })
}

pub fn link_type(kind: &str) -> LinkType {
    LinkType::MEMBERS
        .iter()
        .find(|(v, _)| *v == kind)
        .map(|(_, t)| *t)
        .expect("a link type")
}

/// A memory `n` with `links`, set past the model's own link checks.
pub fn memory(n: u64, links: &[(String, u64, Option<String>)]) -> Memory {
    let p = written::payload(&format!(
        "{{\"memory\": {{\"id\": \"{}\", \"created\": \"2001-02-03T04:05:06.000000Z\", \
             \"updated\": \"2001-02-03T04:05:06.000000Z\", \"scopes\": [\"global\"], \
             \"confidence\": \"high\", \"source\": \"explicit-statement\", \"body\": \"b\"}}}}",
        uid(n)
    ));
    let mut m = Memory::from_payload(p.get("memory").expect("memory")).expect("valid");
    m.links = links
        .iter()
        .map(|(k, t, note)| MemoryLink {
            link_type: link_type(k),
            target_id: unicode::cps(&uid(*t)),
            note: note.as_deref().map(unicode::cps),
        })
        .collect();
    m
}

/// `candidate_link_rows` over the snapshot, ids spelled short.
pub fn answer(snapshot: &Snapshot, wanted: &[u64]) -> Answer {
    let memories: Vec<Memory> = snapshot.iter().map(|(n, l)| memory(*n, l)).collect();
    let ids: Vec<Vec<u32>> = wanted.iter().map(|n| unicode::cps(&uid(*n))).collect();
    let edges = |rows: &[bettermemory_core::response_links::Edge]| -> Vec<Edge> {
        rows.iter()
            .map(|(k, o, note)| {
                (
                    k.to_string(),
                    short(o),
                    note.as_ref()
                        .map(|t| unicode::to_string(t).expect("a note")),
                )
            })
            .collect()
    };
    candidate_link_rows(&memories, &ids)
        .iter()
        .map(|(id, (o, i))| (short(id), edges(o), edges(i)))
        .collect()
}

fn lcg(state: u64) -> u64 {
    state
        .wrapping_mul(6_364_136_223_846_793_005)
        .wrapping_add(1_442_695_040_888_963_407)
}

/// `_sweep_case`: the next state, the snapshot and the wanted ids.
pub fn sweep_case(state: u64) -> (u64, Snapshot, Vec<u64>) {
    let mut state = state;
    let mut draw = |m: u64| {
        state = lcg(state);
        (state >> 33) % m
    };
    let mut snapshot = Snapshot::new();
    for _ in 0..draw(5) {
        let src = draw(6);
        let mut links = Vec::new();
        for _ in 0..draw(3) {
            let kind = KINDS[draw(4) as usize].to_string();
            let target = draw(6);
            let note = [None, Some("a"), Some("b")][draw(3) as usize].map(str::to_string);
            links.push((kind, target, note));
        }
        snapshot.push((src, links));
    }
    let wanted = (0..draw(4)).map(|_| draw(6)).collect();
    (state, snapshot, wanted)
}

/// `_spell`.
pub fn spell(answer: &Answer) -> String {
    let edges = |rows: &[Edge]| {
        rows.iter()
            .map(|(k, o, n)| format!("{k}/{o}/{}", n.as_deref().unwrap_or("-")))
            .collect::<Vec<_>>()
            .join(",")
    };
    answer
        .iter()
        .map(|(id, o, i)| format!("{id}>{}<{}", edges(o), edges(i)))
        .collect::<Vec<_>>()
        .join(";")
}
