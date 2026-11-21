//! The runner of `session.jsonl`'s cases (`gen_session.run`): the scripted
//! clock and counter, each step's answer, and the digest's random cases.

use std::collections::BTreeSet;
use std::sync::Arc;

use bettermemory_core::float_repr;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::session::{
    Effects, Inner, PendingUseToken, SessionRegistry, SessionState, DEFAULT_USE_TOKEN_TTL_TURNS,
};
use serde_json::{json, Map, Value};

pub struct Fx {
    pub now: f64,
    pub n: u64,
    pub reads: u64,
    pub draws: u64,
}

impl Effects for Fx {
    fn time(&mut self) -> f64 {
        self.reads += 1;
        self.now
    }

    fn token_hex8(&mut self) -> String {
        self.draws += 1;
        self.n += 1;
        format!("{:016x}", self.n)
    }
}

pub fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

pub fn text(p: &[u32]) -> String {
    p.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

/// A text as the file writes it: a string, or `{"cps": [...]}` where it
/// holds a lone surrogate (`gen_session._wire`).
pub fn wire(p: &[u32]) -> Value {
    match p
        .iter()
        .map(|&c| char::from_u32(c))
        .collect::<Option<String>>()
    {
        Some(s) => Value::String(s),
        None => json!({ "cps": p }),
    }
}

/// The reverse of `wire`.
pub fn py(v: &Value) -> PyStr {
    match v {
        Value::String(s) => cps(s),
        _ => v["cps"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| u32::try_from(c.as_u64().unwrap()).unwrap())
            .collect(),
    }
}

pub fn float(v: &Value) -> f64 {
    v.as_str().unwrap().parse().unwrap()
}

fn tok(t: &PendingUseToken) -> Value {
    json!([
        wire(&t.memory_id),
        t.token,
        float_repr::repr(t.issued_at),
        t.issued_at_turn
    ])
}

pub fn snap(st: &Inner) -> Value {
    json!({
        "sid": st.session_id,
        "ck": wire(&st.client_key),
        "sc": st.disabled_scopes.iter().map(|s| wire(s)).collect::<Vec<_>>(),
        "p": st.pending_use_tokens.iter().map(|(_, t)| tok(t)).collect::<Vec<_>>(),
        "x": st.expired_use_tokens.iter().map(|(_, t)| tok(t)).collect::<Vec<_>>(),
        "tc": st.turn_counter,
    })
}

fn sorted(s: BTreeSet<PyStr>) -> Value {
    json!(s.iter().map(|x| wire(x)).collect::<Vec<_>>())
}

fn key(v: &Value) -> Option<PyStr> {
    (!v.is_null()).then(|| py(v))
}

/// Each step's `{"r", "n", "k", "st"}`.
pub fn run(case: &Value) -> Vec<Value> {
    let mut fx = Fx {
        now: 0.0,
        n: case["seed"].as_u64().unwrap(),
        reads: 0,
        draws: 0,
    };
    let reg = case
        .get("m")
        .map(|m| SessionRegistry::new(m.as_i64().unwrap()));
    let mut cur: Option<Arc<SessionState>> = None;
    let mut made: Vec<Arc<SessionState>> = Vec::new();
    let mut out = Vec::new();
    for step in case["s"].as_array().unwrap() {
        if let Some(t) = step.get("t") {
            fx.now = float(t);
        }
        fx.reads = 0;
        fx.draws = 0;
        let do_ = step["do"].as_array().unwrap();
        let a = &do_[1..];
        let r: Value = match do_[0].as_str().unwrap() {
            "new" => {
                let s = Arc::new(SessionState::new(&mut fx));
                made.push(Arc::clone(&s));
                let sid = s.lock().session_id.clone();
                cur = Some(s);
                json!(sid)
            }
            "req" => {
                let s = reg.as_ref().unwrap().for_request(key(&a[0]), &mut fx);
                if !made.iter().any(|m| Arc::ptr_eq(m, &s)) {
                    made.push(Arc::clone(&s));
                }
                let i = made.iter().position(|m| Arc::ptr_eq(m, &s)).unwrap();
                let g = s.lock();
                let r = json!({"i": i, "ck": wire(&g.client_key), "sid": g.session_id});
                drop(g);
                cur = Some(s);
                r
            }
            "keys" => {
                let reg = reg.as_ref().unwrap();
                let o: Vec<Value> = reg.keys_in_order().iter().map(|k| wire(k)).collect();
                json!({"o": o, "s": sorted(reg.known_keys())})
            }
            "stats" => {
                let s = reg.as_ref().unwrap().stats();
                json!([
                    ["size", s.size],
                    ["evicted", s.evicted],
                    ["max_clients", s.max_clients]
                ])
            }
            op => {
                let state = cur.as_ref().unwrap();
                let mut g = state.lock();
                match op {
                    "dis" => sorted(g.disable(py(&a[0]))),
                    "ena" => sorted(g.enable(&py(&a[0]))),
                    "adv" => json!(g.advance_turn(&mut fx)),
                    "iss" => {
                        let ids: Vec<PyStr> = a[0].as_array().unwrap().iter().map(py).collect();
                        let got = g.issue_use_tokens(&ids, &mut fx);
                        json!(got
                            .iter()
                            .map(|(m, t)| json!([wire(m), t]))
                            .collect::<Vec<_>>())
                    }
                    "con" => {
                        let kw = &a[0];
                        let ttl = kw
                            .get("ttl")
                            .map_or(DEFAULT_USE_TOKEN_TTL_TURNS, |v| v.as_i64().unwrap());
                        let min = kw
                            .get("min")
                            .and_then(|v| v.as_str())
                            .map(|s| s.parse().unwrap());
                        let ovr: Option<BTreeSet<PyStr>> = kw
                            .get("ovr")
                            .and_then(|v| v.as_array().map(|a| a.iter().map(py).collect()));
                        let got = g.consume_old_tokens(ttl, min, ovr.as_ref(), &mut fx);
                        json!(got.iter().map(|m| wire(m)).collect::<Vec<_>>())
                    }
                    "pur" => json!(g.purge_use_token(&py(&a[0]))),
                    "pop" => json!(g
                        .pop_expired_use_tokens()
                        .iter()
                        .map(tok)
                        .collect::<Vec<_>>()),
                    "rst" => {
                        g.reset();
                        Value::Null
                    }
                    "turn" => {
                        g.turn_counter = a[0].as_i64().unwrap();
                        Value::Null
                    }
                    other => panic!("op {other}"),
                }
            }
        };
        let st = cur.as_ref().map_or(Value::Null, |s| snap(&s.lock()));
        let mut m = Map::new();
        m.insert("r".into(), r);
        m.insert("n".into(), json!(fx.reads));
        m.insert("k".into(), json!(fx.draws));
        m.insert("st".into(), st);
        out.push(Value::Object(m));
    }
    out
}

pub const IDS: [&str; 5] = ["m1", "m2", "m3", "m4", "m5"];
const KEYS: [Option<&str>; 5] = [None, Some("__default__"), Some("a"), Some("b"), Some("c")];
const DELTAS: [f64; 8] = [0.5, 1.0, 599.0, 600.0, 601.0, 1199.0, 1800.0, 1801.0];
const MINS: [Option<&str>; 5] = [
    None,
    Some("0.0"),
    Some("600.0"),
    Some("1800.0"),
    Some("-1.0"),
];

/// `gen_session._random_case`.
pub fn random_case(next: &mut dyn FnMut() -> u64) -> Value {
    let mut r = |n: u64| next() % n;
    let kind = r(5);
    let mut case = json!({"seed": r(1 << 32)});
    let mut steps = Vec::new();
    if kind == 0 {
        steps.push(json!({"do": ["new"]}));
    } else {
        case["m"] = json!(kind - 1);
        steps.push(json!({"do": ["req", KEYS[r(5) as usize]]}));
    }
    let reg = kind != 0;
    let mut t = 1000.0f64;
    for _ in 0..24 {
        let mut step = Map::new();
        if r(3) == 0 {
            t += DELTAS[r(8) as usize];
            step.insert("t".into(), json!(float_repr::repr(t)));
        }
        let do_ = match r(10) {
            0 => json!(["adv"]),
            1 => {
                let n = r(4);
                let ids: Vec<&str> = (0..n).map(|_| IDS[r(5) as usize]).collect();
                json!(["iss", ids])
            }
            2 => {
                let ttl = r(5) as i64 - 1;
                let min = MINS[r(5) as usize];
                let mut kw = json!({"ttl": ttl, "min": min});
                if r(2) != 0 {
                    kw["ovr"] = json!([IDS[r(5) as usize]]);
                }
                json!(["con", kw])
            }
            3 => json!(["pur", IDS[r(5) as usize]]),
            4 => json!(["pop"]),
            5 => json!(["dis", IDS[r(3) as usize]]),
            6 => json!(["ena", IDS[r(3) as usize]]),
            7 => {
                if r(4) == 0 {
                    json!(["rst"])
                } else {
                    json!(["adv"])
                }
            }
            8 => {
                if reg {
                    json!(["req", KEYS[r(5) as usize]])
                } else {
                    json!(["adv"])
                }
            }
            _ => {
                if reg {
                    if r(2) != 0 {
                        json!(["keys"])
                    } else {
                        json!(["stats"])
                    }
                } else {
                    json!(["iss", [IDS[r(5) as usize]]])
                }
            }
        };
        step.insert("do".into(), do_);
        steps.push(Value::Object(step));
    }
    case["s"] = json!(steps);
    case
}

/// splitmix64 from `seed`.
pub fn splitmix(seed: u64) -> impl FnMut() -> u64 {
    let mut state = seed;
    move || {
        state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
}
