//! The scripted reader and git of `gen_repository_history`, shared by the
//! golden, kills and robustness tests of `repository_history`.

use std::collections::HashSet;

use bettermemory_core::gitobjects::{Commit, TreeEntry};
use bettermemory_core::repository_history::{
    Done, GitRun, Objects, RepositoryHistory, NULL_DEVICE,
};
use serde_json::{json, Map, Value};

use super::unhex;

/// The generator's `_Reader` over its case's ``r``.
pub struct Reader(pub Value);

fn obj<'a>(v: &'a Value, key: &str) -> Option<&'a Map<String, Value>> {
    v.get(key).and_then(Value::as_object)
}

impl Reader {
    fn raise(&self, method: &str, arg: &str) -> Result<(), String> {
        let x = self.0.get("x").and_then(Value::as_array);
        let hit = x.is_some_and(|x| {
            x.iter().any(|e| {
                let e = e.as_str().unwrap();
                e == format!("{method}:{arg}") || e == format!("{method}:*")
            })
        });
        if hit {
            Err("ValueError".into())
        } else {
            Ok(())
        }
    }
}

impl Objects for Reader {
    type Raised = String;
    fn complete(&mut self) -> Result<bool, String> {
        self.raise("complete", "")?;
        Ok(self.0.get("c").and_then(Value::as_bool).unwrap_or(true))
    }
    fn name_length(&mut self) -> Result<Option<usize>, String> {
        Ok(match self.0.get("n") {
            None => Some(40),
            Some(v) => v.as_u64().map(|n| n as usize),
        })
    }
    fn has_many(&mut self, shas: &[String]) -> Result<HashSet<String>, String> {
        self.raise("has_many", "")?;
        let o = obj(&self.0, "o");
        Ok(shas
            .iter()
            .filter(|s| o.is_some_and(|o| o.contains_key(*s)))
            .cloned()
            .collect())
    }
    fn resolve(&mut self, prefix: &str) -> Result<Option<String>, String> {
        self.raise("resolve", prefix)?;
        if prefix.chars().count() < 4 {
            return Ok(None);
        }
        let low = prefix.to_lowercase();
        let hits: Vec<&String> = obj(&self.0, "o")
            .map(|o| o.keys().filter(|k| k.starts_with(&low)).collect())
            .unwrap_or_default();
        Ok((hits.len() == 1).then(|| hits[0].clone()))
    }
    fn read(&mut self, sha: &str) -> Result<Option<(String, Vec<u8>)>, String> {
        self.raise("read", sha)?;
        Ok(obj(&self.0, "o").and_then(|o| o.get(sha)).map(|g| {
            (
                g[0].as_str().unwrap().to_owned(),
                unhex(g[1].as_str().unwrap()),
            )
        }))
    }
    fn commit(&mut self, sha: &str) -> Result<Option<Commit>, String> {
        self.raise("commit", sha)?;
        Ok(obj(&self.0, "ct").and_then(|o| o.get(sha)).map(|t| Commit {
            tree: t.as_str().unwrap().to_owned(),
            parents: Vec::new(),
            author: Vec::new(),
            committer: Vec::new(),
            message: Vec::new(),
        }))
    }
    fn tree(&mut self, sha: &str) -> Result<Option<Vec<TreeEntry>>, String> {
        self.raise("tree", sha)?;
        Ok(obj(&self.0, "tr").and_then(|o| o.get(sha)).map(|g| {
            g.as_array()
                .unwrap()
                .iter()
                .map(|e| TreeEntry {
                    mode: e[0].as_u64().unwrap() as u32,
                    name: e[1].as_str().unwrap().as_bytes().to_vec(),
                    sha: e[2].as_str().unwrap().to_owned(),
                })
                .collect()
        }))
    }
    fn header(&mut self, sha: &str) -> Result<Option<(String, u64)>, String> {
        self.raise("header", sha)?;
        Ok(obj(&self.0, "sz").and_then(|o| o.get(sha)).map(|s| {
            // A size past u64 (the generator's 2**70) is at least the floor.
            ("blob".to_owned(), s.as_u64().unwrap_or(u64::MAX))
        }))
    }
}

/// The generator's scripted git: answers in order, every call recorded.
pub struct Script {
    pub answers: Vec<Value>,
    pub events: Vec<Value>,
}

impl GitRun for Script {
    fn run(
        &mut self,
        args: &[String],
        git_dir: &str,
        stdin: Option<&[u8]>,
        timeout: f64,
        env: &[(String, String)],
        text: bool,
    ) -> Option<Done> {
        let env = if env.is_empty() {
            Value::Null
        } else {
            Value::Object(
                env.iter()
                    .map(|(k, v)| {
                        let v = if v == NULL_DEVICE { "<devnull>" } else { v };
                        (k.clone(), json!(v))
                    })
                    .collect(),
            )
        };
        let stdin = stdin.map(|s| String::from_utf8(s.to_vec()).unwrap());
        self.events
            .push(json!(["run", args, stdin, timeout, env, text, git_dir]));
        assert!(
            !self.answers.is_empty(),
            "the case runs git more often than it scripts"
        );
        let got = self.answers.remove(0);
        if got.is_null() {
            return None;
        }
        let stdout = match got.get("ox") {
            Some(x) => unhex(x.as_str().unwrap()),
            None => got
                .get("o")
                .and_then(Value::as_str)
                .unwrap_or("")
                .as_bytes()
                .to_vec(),
        };
        Some(Done {
            returncode: got.get("rc").and_then(Value::as_i64).unwrap_or(0),
            stdout,
            stderr: got
                .get("e")
                .and_then(Value::as_str)
                .unwrap_or("")
                .as_bytes()
                .to_vec(),
        })
    }
    fn log_nonzero_exit(&mut self, args: &[String], done: &Done, at: &str) {
        self.events.push(json!(["log", args, done.returncode, at]));
    }
}

pub type Factory = Box<dyn FnMut(&str) -> Result<Reader, String>>;
pub type History = RepositoryHistory<Reader, Factory, Script>;

/// A history over a case's ``r`` and ``g``, made at ``C``.
pub fn history(case: &Value) -> History {
    let spec = case.get("r").cloned().unwrap_or_else(|| json!({}));
    let mut fails = spec.get("mk").and_then(Value::as_u64).unwrap_or(0);
    let make: Factory = Box::new(move |common: &str| {
        assert_eq!(common, "C");
        if fails > 0 {
            fails -= 1;
            return Err("OSError".into());
        }
        Ok(Reader(spec.clone()))
    });
    let answers = case
        .get("g")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    RepositoryHistory::new(
        "C",
        make,
        Script {
            answers,
            events: Vec::new(),
        },
    )
}

fn strs(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| s.as_str().unwrap().to_owned())
        .collect()
}

fn raised<T>(got: Result<T, String>, f: impl FnOnce(T) -> Value) -> Value {
    match got {
        Ok(v) => f(v),
        Err(e) => json!({ "raised": e }),
    }
}

/// One step's answer, spelled as the generator's `_answer`.
pub fn answer(h: &History, method: &str, arg: &Value) -> Value {
    match method {
        "expand" => raised(h.expand(&strs(arg)), |v| json!(v)),
        "_expand" => raised(h.expand_typed(&strs(arg)), |v| {
            json!(v
                .into_iter()
                .map(|(g, (f, k))| json!([g, f, k]))
                .collect::<Vec<_>>())
        }),
        "holds" => raised(h.holds(&strs(arg)), |v| json!(v)),
        "holds_objects" => raised(h.holds_objects(&strs(arg)), |v| json!(v)),
        "fingerprint" => raised(h.fingerprint(arg.as_str().unwrap()), |v| json!(v)),
        "parents" => raised(h.parents(arg.as_str().unwrap()), |v| json!(v)),
        "committed" => raised(h.committed(arg.as_str().unwrap()), |v| json!(v)),
        "reach" => raised(h.reachable_among(&strs(arg)), |r| {
            json!([r.relations, r.bounded, r.handled])
        }),
        "independent" => json!(h.independent(&strs(arg))),
        "blobs" => raised(h.blobs(arg.as_str().unwrap()), |v| json!(v)),
        _ => panic!("unknown method {method}"),
    }
}

/// Every step of a case: (answer, git calls) as the generator spells them.
pub fn run(case: &Value) -> Vec<Value> {
    let h = history(case);
    let mut out = Vec::new();
    for step in case["s"].as_array().unwrap() {
        let a = answer(&h, step[0].as_str().unwrap(), &step[1]);
        let q = h.with_run(|r| std::mem::take(&mut r.events));
        out.push(json!({"a": a, "q": q}));
    }
    assert!(
        h.with_run(|r| r.answers.is_empty()),
        "the case scripts git answers it never reads"
    );
    out
}
