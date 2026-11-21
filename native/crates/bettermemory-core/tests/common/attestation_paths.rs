//! What the attestation path tests share (`golden_attestation_paths`,
//! `golden_attestation_paths_posix`, the kills and robustness tests): the
//! scripted host `gen_attestation_paths` describes, the answers spelled as
//! its `spell` spells them, and its seeded sweep.

use bettermemory_core::attestation_paths::{
    absolute_spelling, interpret_path, joined_path, literal_path, placeholder_spelling,
    reference_path, target_path, Host, Mode, Raised, Targets,
};
use bettermemory_core::fsdata::{Flavour, PyErr};
use bettermemory_core::gitfile_rule::hostpath::text::normcase;
use bettermemory_core::pyvalue::PyStr;

pub struct Scripted {
    pub nt: bool,
}

pub fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

impl Host for Scripted {
    fn nt(&self) -> bool {
        self.nt
    }

    fn expanduser(&self, p: &[u32]) -> Result<PyStr, Raised> {
        let home = if self.nt { "C:\\Hm\\u" } else { "/hm/u" };
        let tilde = u32::from(b'~');
        if p.first() == Some(&tilde) && (p.len() == 1 || p[1] == 47 || p[1] == 92) {
            let mut out = cps(home);
            out.extend_from_slice(&p[1..]);
            return Ok(out);
        }
        if p.starts_with(&cps("~z")) {
            return Err(Raised::Value(cps("scripted")));
        }
        if p.starts_with(&cps("~!")) {
            return Err(Raised::Other("KeyError".into()));
        }
        Ok(p.to_vec())
    }

    fn lstat(&self, p: &[u32]) -> Result<(), Raised> {
        if p.contains(&0) {
            return Err(Raised::Value(Vec::new()));
        }
        match p.last().and_then(|&c| char::from_u32(c)) {
            Some('a') => Ok(()),
            Some('b') => Err(Raised::Os(PyErr::Permission)),
            Some('d') => Err(Raised::Os(PyErr::NotADirectory)),
            Some('e') => Err(Raised::Os(PyErr::Os)),
            _ => Err(Raised::Os(PyErr::FileNotFound)),
        }
    }

    /// The generator's host keeps the module's own `ntpath.normcase`.
    fn nt_normcase(&self, text: &[u32]) -> Result<PyStr, Raised> {
        Ok(normcase(Flavour::Windows, text))
    }
}

pub fn text(s: &[u32]) -> String {
    // A lone surrogate is kept as U+FFFD's place holder only for digests,
    // which go through `utf8_surrogatepass`; here every text is valid.
    s.iter()
        .map(|&c| char::from_u32(c).unwrap_or('\u{fffd}'))
        .collect()
}

pub fn spell_str(s: &[u32]) -> PyStr {
    let mut out = cps("S");
    out.extend_from_slice(s);
    out
}

pub fn spell_opt(s: &Option<PyStr>) -> PyStr {
    s.as_ref().map_or_else(|| cps("N"), |s| spell_str(s))
}

pub fn spell_bool(b: bool) -> PyStr {
    cps(if b { "T" } else { "F" })
}

pub fn spell_list(items: &[PyStr]) -> PyStr {
    let mut out = cps("L");
    let parts: Vec<PyStr> = items.iter().map(|s| spell_str(s)).collect();
    out.extend(parts.join(&0x1d));
    out
}

pub fn spell_targets(t: &Targets) -> PyStr {
    let mut out = cps("D");
    let parts: Vec<PyStr> = t
        .iter()
        .map(|(k, v)| {
            let mut p = spell_str(k);
            p.push(u32::from(b'='));
            p.extend(spell_opt(v));
            p
        })
        .collect();
    out.extend(parts.join(&0x1d));
    out
}

pub fn raised(e: Raised) -> PyStr {
    let name = match e {
        Raised::Index => "IndexError".to_owned(),
        Raised::Value(_) => "ValueError".to_owned(),
        Raised::Os(c) => c.name().to_owned(),
        Raised::Other(c) => c,
        Raised::NotPorted => panic!("declined"),
    };
    cps(&format!("!{name}"))
}

pub fn spell<T>(r: Result<T, Raised>, f: impl FnOnce(T) -> PyStr) -> PyStr {
    r.map_or_else(raised, f)
}

pub fn spell_compiled(r: Result<(Vec<PyStr>, Targets), Raised>) -> PyStr {
    spell(r, |(legacy, targets)| {
        let mut out = cps("L");
        out.extend(spell_list(&legacy));
        out.push(0x1d);
        out.extend(spell_targets(&targets));
        out
    })
}

pub const MODES: [Mode; 3] = [Mode::Auto, Mode::Literal, Mode::Reference];
pub const ROOTS: [Option<&str>; 8] = [
    None,
    Some("/r"),
    Some("r/s"),
    Some("C:\\r"),
    Some("c:/R/"),
    Some("\\\\srv\\sh\\r"),
    Some("/"),
    Some(""),
];
pub const NAMED_ROOTS: [Option<&str>; 3] = [None, Some("/r"), Some("C:\\r")];

/// `_per_input(s, roots)`.
pub fn per_input(host: &dyn Host, s: &[u32], roots: &[Option<&str>]) -> Vec<PyStr> {
    let mut out = vec![
        spell_bool(absolute_spelling(s)),
        spell_opt(&literal_path(s)),
        spell(reference_path(s), |v| spell_opt(&v)),
    ];
    for root in roots {
        let root = root.map(cps);
        let root = root.as_deref();
        out.push(spell(target_path(host, s, root), |v| spell_str(&v)));
        for mode in MODES {
            out.push(spell(interpret_path(host, s, mode, root), |v| {
                spell_opt(&v)
            }));
            out.push(spell(placeholder_spelling(host, s, mode, root), spell_bool));
            if let Some(root) = root {
                out.push(spell(joined_path(host, root, s, mode), |v| spell_opt(&v)));
            }
        }
    }
    out
}

/// `str.encode("utf-8", "surrogatepass")`.
pub fn surrogatepass(s: &[u32]) -> Vec<u8> {
    let mut out = Vec::new();
    for &c in s {
        match char::from_u32(c) {
            Some(ch) => out.extend_from_slice(ch.encode_utf8(&mut [0; 4]).as_bytes()),
            None => out.extend([
                0xE0 | (c >> 12) as u8,
                0x80 | ((c >> 6) & 0x3F) as u8,
                0x80 | (c & 0x3F) as u8,
            ]),
        }
    }
    out
}

const PIECES: [&str; 37] = [
    "a",
    "b",
    "d",
    "e",
    "x",
    ".",
    "..",
    "/",
    "\\",
    ":",
    "C:",
    "c:/",
    "~",
    "~z",
    "~!",
    "$HOME/",
    "${HOME}/",
    " ",
    "*",
    "<",
    "\"",
    "|",
    "?",
    "\0",
    "/path/to/",
    "<path>",
    "\u{e9}",
    "\n",
    "//",
    "foo",
    "@",
    "://",
    "-",
    "\t",
    "src/",
    "x.py",
    "~/",
];

struct Lcg(u64);

impl Lcg {
    fn draw(&mut self, n: usize) -> usize {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        ((self.0 >> 33) as usize) % n
    }
}

pub fn sweep_inputs(seed: u64, count: usize) -> Vec<PyStr> {
    let mut rng = Lcg(seed);
    (0..count)
        .map(|_| {
            let length = 1 + rng.draw(6);
            (0..length)
                .flat_map(|_| cps(PIECES[rng.draw(PIECES.len())]))
                .collect()
        })
        .collect()
}
