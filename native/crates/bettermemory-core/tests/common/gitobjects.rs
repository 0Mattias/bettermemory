//! What the tests of `gitobjects::stream` share with
//! `tests/contract/gen_gitobjects_streams.py`: bytes as the fixture spells
//! them, a pack index built from its listing, the scripted file handle,
//! and the random source, the mutations and the two sweep rules, each
//! restated here step for step; and the time limit a test runs under.

use std::fmt::Write as _;
use std::panic;
use std::sync::mpsc::{self, RecvTimeoutError};
use std::thread;
use std::time::Duration;

use bettermemory_core::fsdata::PyErr;
use bettermemory_core::gitobjects::stream::{self, Failure, Handle, Link, Stream};
use bettermemory_core::gitobjects::{parse_index, varint, PackIndex, Raised, Wide};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// Bytes as the fixture spells them: hex text, or pieces, each hex text
/// or `[hex, count]`, a unit repeated.
pub fn content(v: &Value) -> Vec<u8> {
    match v {
        Value::String(s) => super::unhex(s),
        Value::Array(pieces) => {
            let mut out = Vec::new();
            for piece in pieces {
                match piece {
                    Value::String(s) => out.extend(super::unhex(s)),
                    Value::Array(run) => {
                        let unit = super::unhex(run[0].as_str().unwrap());
                        out.extend(unit.repeat(run[1].as_u64().unwrap() as usize));
                    }
                    other => panic!("a piece {other}"),
                }
            }
            out
        }
        other => panic!("bytes spelled {other}"),
    }
}

pub fn sha(data: &[u8]) -> String {
    super::hex(&Sha256::digest(data))
}

/// A `Wide` from `format(value, "x")`, through the varint that spells
/// the same value.
pub fn wide(hex: &str) -> Wide {
    let mut bits = Vec::new();
    for digit in hex.bytes().rev() {
        let d = (digit as char).to_digit(16).expect("a hex digit") as u8;
        bits.extend((0..4).map(|i| (d >> i) & 1));
    }
    let mut groups: Vec<u8> = bits
        .chunks(7)
        .map(|group| 0x80 | group.iter().rev().fold(0, |v, b| (v << 1) | b))
        .collect();
    *groups.last_mut().expect("a digit") &= 0x7f;
    varint(&groups, 0).expect("a whole varint").0
}

/// The pack index of a listing `{"h": name length, "ix": [[name,
/// offset], ...]}`, by the fixture's rule: the fanout, the names in
/// order, zero CRCs, each offset in the 31-bit table or, from 2**31, in
/// the 64-bit table in order of appearance; `["raw", n]` is `n` in the
/// 31-bit table as it is.
pub fn index(spec: &Value) -> PackIndex {
    let hash_len = spec["h"].as_u64().unwrap() as usize;
    let listed = spec["ix"].as_array().unwrap();
    let names: Vec<Vec<u8>> = listed
        .iter()
        .map(|e| super::unhex(e[0].as_str().unwrap()))
        .collect();
    let mut data = b"\xfftOc\x00\x00\x00\x02".to_vec();
    for i in 0..256usize {
        let count = if hash_len == 0 {
            0
        } else {
            names.iter().filter(|n| usize::from(n[0]) <= i).count() as u32
        };
        data.extend(count.to_be_bytes());
    }
    for name in &names {
        data.extend(name);
    }
    data.extend(vec![0u8; 4 * names.len()]);
    let mut large: Vec<u64> = Vec::new();
    for entry in listed {
        let small = match &entry[1] {
            Value::Array(raw) => raw[1].as_u64().unwrap() as u32,
            offset => match offset.as_u64().unwrap() {
                at if at < 0x8000_0000 => at as u32,
                at => {
                    large.push(at);
                    0x8000_0000 | (large.len() as u32 - 1)
                }
            },
        };
        data.extend(small.to_be_bytes());
    }
    for at in large {
        data.extend(at.to_be_bytes());
    }
    data.extend(vec![0u8; 2 * hash_len]);
    parse_index(data, b"p.pack".to_vec(), hash_len).expect("an index git accepts")
}

/// The offsets a listing gives outright (the sweep's `offsets`).
pub fn listed_offsets(spec: &Value) -> Vec<u64> {
    spec["ix"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|e| e[1].as_u64())
        .collect()
}

/// The reads a `File` answers before it fails the test. No case reads one
/// handle 8,192 times; a stream whose loop does not end reads on without
/// end, and fails here at once instead of growing the log until the run
/// is stopped.
const MOST_READS: u64 = 1 << 20;

/// The fixture's handle: bytes at `origin`, a script of reads (a positive
/// entry caps that read, 0 answers nothing, -1 is OSError, -2
/// PermissionError), a seek that raises, and the log of its calls. Past
/// [`MOST_READS`] reads it fails the test, which `_File` never needs to.
pub struct File {
    pub data: Vec<u8>,
    pub script: Vec<i64>,
    pub origin: u64,
    pub bad_seek: Option<u64>,
    pub pos: u64,
    pub reads: u64,
    pub seeks: u64,
    pub log: String,
}

impl File {
    pub fn new(data: Vec<u8>, script: Vec<i64>) -> File {
        File {
            data,
            script,
            origin: 0,
            bad_seek: None,
            pos: 0,
            reads: 0,
            seeks: 0,
            log: String::new(),
        }
    }

    fn note(&mut self, kind: char, n: u64) {
        if !self.log.is_empty() {
            self.log.push(' ');
        }
        write!(self.log, "{kind}{n}").unwrap();
    }

    /// The log as the fixture writes it: the text, or past 400
    /// characters its count of calls and its sha256.
    pub fn logged(&self) -> Value {
        if self.log.len() <= 400 {
            return Value::String(self.log.clone());
        }
        let calls = self.log.split(' ').count();
        serde_json::json!({ "calls": calls, "sha": sha(self.log.as_bytes()) })
    }
}

impl Handle for File {
    fn seek(&mut self, offset: u64) -> Result<(), PyErr> {
        self.note('s', offset);
        self.seeks += 1;
        if self.bad_seek == Some(self.seeks - 1) {
            return Err(PyErr::Os);
        }
        self.pos = offset;
        Ok(())
    }

    fn read(&mut self, n: usize) -> Result<Vec<u8>, PyErr> {
        assert!(
            self.reads < MOST_READS,
            "{MOST_READS} reads of one handle: a stream's loop that does not end"
        );
        self.note('r', n as u64);
        let rule = if self.script.is_empty() {
            n as i64
        } else {
            self.script[(self.reads % self.script.len() as u64) as usize]
        };
        self.reads += 1;
        match rule {
            -1 => return Err(PyErr::Os),
            -2 => return Err(PyErr::Permission),
            0 => return Ok(Vec::new()),
            _ => {}
        }
        let Some(at) = self.pos.checked_sub(self.origin) else {
            return Ok(Vec::new());
        };
        if at >= self.data.len() as u64 {
            return Ok(Vec::new());
        }
        let at = at as usize;
        let end = at + n.min(rule as usize).min(self.data.len() - at);
        self.pos += (end - at) as u64;
        Ok(self.data[at..end].to_vec())
    }
}

/// Runs `body` on a thread of its own and fails the test where it has
/// not returned within `limit`: a stream's loop that does not end over a
/// handle that sets no bound of its own (`MemHandle`), or that grows its
/// answer without end, fails the run instead of hanging it. A panic in
/// `body` is the test's.
pub fn within<T: Send + 'static>(limit: Duration, body: impl FnOnce() -> T + Send + 'static) -> T {
    let (sent, got) = mpsc::channel();
    let worker = thread::spawn(move || {
        // Nothing receives once the limit has passed.
        let _ = sent.send(body());
    });
    match got.recv_timeout(limit) {
        Ok(value) => {
            let _ = worker.join();
            value
        }
        Err(RecvTimeoutError::Timeout) => {
            panic!("not done within {limit:?}: a loop that does not end")
        }
        Err(RecvTimeoutError::Disconnected) => match worker.join() {
            Err(payload) => panic::resume_unwind(payload),
            Ok(()) => unreachable!("the body ended without an answer"),
        },
    }
}

/// A raise as the fixture spells it: `["z", code]` for `zlib.error`,
/// `["x", class]` for the rest.
pub fn raised(failure: Failure) -> Value {
    match failure {
        Failure::Zlib(code) => serde_json::json!(["z", code]),
        Failure::Os(class) => serde_json::json!(["x", class.name()]),
        Failure::Overflow => serde_json::json!(["x", "OverflowError"]),
        Failure::Raised(r) => serde_json::json!(["x", raise_name(r)]),
    }
}

pub fn raise_name(r: Raised) -> &'static str {
    match r {
        Raised::ValueError => "ValueError",
        Raised::IndexError => "IndexError",
        Raised::StructError => "struct.error",
    }
}

/// An answer of bytes or None as the fixture spells it: `["b", hex]`,
/// `["B", length, sha256]` past 48 bytes, `["n"]`.
pub fn data(value: Option<&[u8]>) -> Value {
    match value {
        None => serde_json::json!(["n"]),
        Some(b) if b.len() <= 48 => serde_json::json!(["b", super::hex(b)]),
        Some(b) => serde_json::json!(["B", b.len(), sha(b)]),
    }
}

/// `_ops`: a stream over `fh` and its calls in turn, each answer with
/// the handle's reads so far and its position.
pub fn ops(fh: &mut File, chunk: usize, calls: &[(bool, u64)]) -> Vec<Value> {
    // The stream borrows the handle for its life, so each call's count
    // and position are read through a recording wrapper.
    struct Counted<'a>(&'a mut File, std::rc::Rc<std::cell::Cell<(u64, u64)>>);
    impl Handle for Counted<'_> {
        fn seek(&mut self, offset: u64) -> Result<(), PyErr> {
            self.0.seek(offset)
        }
        fn read(&mut self, n: usize) -> Result<Vec<u8>, PyErr> {
            let out = self.0.read(n);
            self.1.set((self.0.reads, self.0.pos));
            out
        }
    }
    let seen = std::rc::Rc::new(std::cell::Cell::new((fh.reads, fh.pos)));
    let mut counted = Counted(fh, seen.clone());
    let mut stream = Stream::new(&mut counted, chunk);
    let mut out = Vec::new();
    for &(to_end, n) in calls {
        let answer = if to_end {
            stream.read_to_end(n).map(|got| data(got.as_deref()))
        } else {
            stream.read(n).map(|got| data(Some(&got)))
        };
        let (reads, pos) = seen.get();
        out.push(serde_json::json!([
            answer.unwrap_or_else(raised),
            reads,
            pos
        ]));
    }
    out
}

/// `_links_text`: a walk's answer as one line.
pub fn links_text(code: u8, links: &[Link]) -> String {
    let mut out = code.to_string();
    for link in links {
        let target = link.target.as_ref().map_or("-".into(), Wide::hex);
        write!(out, ";{},{},{target}", link.start, link.size.hex()).unwrap();
    }
    out
}

/// The sweeps' random source: splitmix64.
pub struct Rng(pub u64);

impl Rng {
    pub fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    pub fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }

    pub fn pick<T: Copy>(&mut self, seq: &[T]) -> T {
        seq[self.below(seq.len())]
    }
}

/// `_mutate`: one mutation of `data`.
pub fn mutate(rng: &mut Rng, data: &[u8]) -> Vec<u8> {
    let mut b = data.to_vec();
    let kind = rng.below(12);
    if kind == 0 || b.is_empty() {
        return b;
    }
    let flip = |rng: &mut Rng, b: &mut Vec<u8>, i: usize| b[i] ^= 1 << rng.below(8);
    match kind {
        1 => b.truncate(rng.below(b.len() + 1)),
        2 => {
            let i = rng.below(b.len());
            flip(rng, &mut b, i);
        }
        3 => {
            let i = rng.below(b.len());
            b[i] = rng.below(256) as u8;
        }
        4 => {
            for _ in 0..rng.pick(&[1, 2, 5, 40]) {
                b.push(rng.below(256) as u8);
            }
        }
        5 => {
            b.remove(rng.below(b.len()));
        }
        6 => {
            let i = rng.below(b.len() + 1);
            b.insert(i, rng.below(256) as u8);
        }
        7 => {
            for _ in 0..2 + rng.below(4) {
                let i = rng.below(b.len());
                flip(rng, &mut b, i);
            }
        }
        8 => {
            b.truncate(rng.below(b.len() + 1));
            for _ in 0..1 + rng.below(30) {
                b.push(rng.below(256) as u8);
            }
        }
        9 => {
            let i = rng.below(b.len().min(6));
            flip(rng, &mut b, i);
        }
        10 => {
            let i = b.len() - 1 - rng.below(b.len().min(6));
            flip(rng, &mut b, i);
        }
        _ => b.extend_from_slice(data),
    }
    b
}

/// `_mutated`: `data` after none, one or two mutations.
pub fn mutated(rng: &mut Rng, data: &[u8], most: usize) -> Vec<u8> {
    let times = [0, 1, 1, 2][rng.below(most)];
    let mut out = data.to_vec();
    for _ in 0..times {
        out = mutate(rng, &out);
    }
    out
}

/// `_scripted`: no script, or one of a few entries with a positive one.
pub fn scripted(rng: &mut Rng, odds: usize, entries: &[i64], most: usize) -> Vec<i64> {
    if rng.below(odds) != 0 {
        return Vec::new();
    }
    let mut script: Vec<i64> = (0..1 + rng.below(most))
        .map(|_| rng.pick(entries))
        .collect();
    if script.iter().all(|&k| k <= 0) {
        script.push(7);
    }
    script
}

/// `_brief`: an answer as the sweeps' lines write it.
pub fn brief(answer: &Value) -> String {
    match answer[0].as_str().unwrap() {
        "b" => {
            let value = super::unhex(answer[1].as_str().unwrap());
            format!("b{}.{}", value.len(), &sha(&value)[..16])
        }
        "B" => format!("b{}.{}", answer[1], &answer[2].as_str().unwrap()[..16]),
        _ => answer
            .as_array()
            .unwrap()
            .iter()
            .map(|part| match part {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            })
            .collect(),
    }
}

const CHUNKS: [usize; 11] = [1, 2, 3, 7, 13, 64, 65, 100, 257, 1024, 65536];
const SCRIPT: [i64; 10] = [0, 1, 2, 5, 6, 14, 15, 16, 100, -1];
const CAPS: [Option<u64>; 10] = [
    None,
    None,
    None,
    Some(0),
    Some(1),
    Some(12),
    Some(40),
    Some(4096),
    Some(4 << 20),
    Some(u64::MAX),
];
const FAR: [u64; 4] = [(1 << 63) - 1, 1 << 63, u64::MAX, 0];
const PEEKS: [i64; 7] = [0, 1, 3, 20, 63, 64, -1];

/// `_sweep_stream`: one case of the stream sweep, as its line.
pub fn sweep_stream(rng: &mut Rng, bases: &[(Vec<u8>, u64)]) -> String {
    let (stream, total) = &bases[rng.below(bases.len())];
    let total = *total;
    let data = mutated(rng, stream, 4);
    let chunk = rng.pick(&CHUNKS);
    let script = scripted(rng, 5, &SCRIPT, 4);
    let mut sizes: Vec<u64> = vec![
        0, 1, 2, 20, 32, 100, 257, 258, 259, 1000, 32768, 32769, 65536,
    ];
    sizes.extend([
        total,
        total.saturating_sub(1),
        total + 1,
        1_000_000_000,
        (total / 2).max(1),
    ]);
    let shape = rng.below(10);
    let calls: Vec<(bool, u64)> = match shape {
        0..=2 => vec![(false, 32), (true, total.saturating_sub(32))],
        3 => vec![(true, total)],
        4 => vec![(false, 20)],
        5 => vec![(false, 1); (total + 3).min(12) as usize],
        _ => (0..1 + rng.below(4))
            .map(|_| {
                let to_end = rng.below(4) == 0;
                (to_end, rng.pick(&sizes))
            })
            .collect(),
    };
    let mut fh = File::new(data, script);
    let answers = ops(&mut fh, chunk, &calls);
    let lines: Vec<String> = answers
        .iter()
        .map(|a| format!("{}/{}/{}", brief(&a[0]), a[1], a[2]))
        .collect();
    lines.join(",")
}

/// `_sweep_chain`: one case of the chain sweep, as its line.
pub fn sweep_chain(rng: &mut Rng, packs: &[(Vec<u64>, Vec<u8>, PackIndex)]) -> String {
    let (offsets, raw, index) = &packs[rng.below(packs.len())];
    let data = mutated(rng, raw, 3);
    let at = match rng.below(8) {
        0..=4 => rng.pick(offsets),
        5 => rng.below(data.len() + 2) as u64,
        6 => rng.pick(&FAR),
        _ => rng.pick(offsets).wrapping_add(1),
    };
    let cap = rng.pick(&CAPS);
    let mut fh = File::new(data, scripted(rng, 8, &PEEKS, 3));
    let logged = |fh: &File| sha(fh.log.as_bytes())[..16].to_string();
    let (code, links) = match stream::chain(&mut fh, index, at, cap) {
        Err(failure) => return format!("{}|{}", brief(&raised(failure)), logged(&fh)),
        Ok(None) => return format!("n|{}", logged(&fh)),
        Ok(Some(walked)) => walked,
    };
    let mut parts = vec![links_text(code, &links)];
    for link in links.iter().take(6) {
        parts.push(match stream::inflate_at(&mut fh, link) {
            Err(failure) => brief(&raised(failure)),
            Ok(got) => brief(&data_of(got)),
        });
    }
    format!("{}|{}", parts.join("&"), logged(&fh))
}

fn data_of(got: Option<Vec<u8>>) -> Value {
    data(got.as_deref())
}

/// The sha256 of `n` lines of a sweep from `seed`.
pub fn sweep(seed: u64, n: u64, mut one: impl FnMut(&mut Rng) -> String) -> String {
    let mut rng = Rng(seed);
    let mut digest = Sha256::new();
    for _ in 0..n {
        digest.update(one(&mut rng).as_bytes());
        digest.update(b"\n");
    }
    super::hex(&digest.finalize())
}
